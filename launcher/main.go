// Command aitmation-launcher manages the lifecycle of an aitmation
// household (ADR-0024): it reads household.toml, starts one core instance
// per mandant plus the mailroom, keeps them alive, writes the mailroom's
// addressee table, and answers a small HTTP API the product shell's setup
// wizard uses to add mandants. Release fetching, signature verification,
// updates with rollback and backups (ADR-0002, ADR-0003, ADR-0013) are the
// next phase and marked TODO below.
//
// The core does not know about the launcher. It starts, reads its
// configuration and opens a port – whether it was started by a launcher,
// Docker or a human is irrelevant to it (see ADR-0002, ADR-0011).
//
// Usage:
//
//	aitmation-launcher init  [-data-dir D] [-admin NAME] [-admin-password P] [-private] [-company "Firma X"]...
//	aitmation-launcher run   [-data-dir D] [-core-dir core/] [-apps-dir apps/] [-core-cmd "uv run python -m aitmation_core"]
//	aitmation-launcher user  add|remove|passwd [-data-dir D] -name N [-password P] [-role admin|member] [-instances a,b]
//	aitmation-launcher token [-data-dir D] -user N [-days 365]     (for the MCP channel and other clients)
//
// Identity (ADR-0025): the launcher keeps the household's users and secret
// and hands each core the secret; cores verify tokens, nothing more.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/aitmation/aitmation/launcher/internal/api"
	"github.com/aitmation/aitmation/launcher/internal/auth"
	"github.com/aitmation/aitmation/launcher/internal/bootstrap"
	"github.com/aitmation/aitmation/launcher/internal/household"
	"github.com/aitmation/aitmation/launcher/internal/supervise"
)

// version is stamped by the bundle build (-ldflags "-X main.version=...").
var version = "dev"

// Config is fed from flags and environment variables; environment wins
// where both exist so container operation works without a file.
type Config struct {
	DataDir string   // root: household.toml, mandants/<id>/, mailroom/, backups/
	CoreDir string   // checkout or release directory containing the core
	AppsDir string   // catalog source (apps/)
	CoreCmd []string // how to start a core in CoreDir
}

// resolveCoreCmd prefers the interpreter of a synced virtualenv in coreDir
// over "uv run": one process instead of a uv parent, which matters where
// killing the parent would orphan the core (Windows), and it works without
// uv on PATH. A release bundle has no environment yet - bootstrap creates
// it on first start (needs the network once).
func resolveCoreCmd(ctx context.Context, coreDir, dataDir string, fallback []string, log *slog.Logger) []string {
	py, err := bootstrap.EnsureVenv(ctx, coreDir, dataDir, os.Stdout)
	if err != nil {
		log.Warn("no Python environment for the core; falling back to `uv run`", "error", err)
		return fallback
	}
	if py == bootstrap.BundledPython(coreDir) {
		// The bundle's own interpreter (ADR-0051): the core sources are not
		// installed into it, they are imported from <core>/src.
		for k, v := range bootstrap.CoreEnv(coreDir) {
			if v == "" {
				os.Unsetenv(k)
			} else {
				os.Setenv(k, v)
			}
		}
		log.Info("core runs on the bundled interpreter", "python", py)
	}
	return []string{py, "-m", "aitmation_core"}
}

func defaultConfig() Config {
	cfg := Config{
		DataDir: os.Getenv("AITMATION_DATA_DIR"),
		CoreDir: os.Getenv("AITMATION_CORE_DIR"),
		AppsDir: os.Getenv("AITMATION_APPS_DIR"),
		CoreCmd: []string{"uv", "run", "python", "-m", "aitmation_core"},
	}
	if cfg.DataDir == "" {
		if home, err := os.UserHomeDir(); err == nil {
			cfg.DataDir = filepath.Join(home, ".aitmation")
		} else {
			cfg.DataDir = ".aitmation"
		}
	}
	return cfg
}

// Launcher runs one household.
type Launcher struct {
	cfg  Config
	log  *slog.Logger
	path string // household.toml
	mu   sync.Mutex
	hh   *household.Household
	sup  *supervise.Supervisor
	ctx  context.Context
	auth *auth.Store // nil: household without users, authentication off

	openBrowser bool
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// openInBrowser is best effort: a bundle started by double-click should
// land the person in the shell without typing an address.
func openInBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}

func (l *Launcher) Auth() *auth.Store { return l.auth }

// serviceHeaders authenticate the launcher's own calls to the cores.
func (l *Launcher) serviceHeaders() map[string]string {
	if l.auth == nil {
		return nil
	}
	return map[string]string{"Authorization": "Bearer " + l.auth.ServiceToken()}
}

func (l *Launcher) Household() *household.Household {
	l.mu.Lock()
	defer l.mu.Unlock()
	copy := *l.hh
	copy.Mandants = append([]household.Mandant(nil), l.hh.Mandants...)
	return &copy
}

func (l *Launcher) Statuses() []supervise.Status { return l.sup.Statuses() }

// ShellURL is the shell behind the launcher's own origin (ADR-0032); the
// mailroom still serves it directly on its port for development.
func (l *Launcher) ShellURL() string {
	return fmt.Sprintf("http://127.0.0.1:%d/app/", l.hh.Launcher.Port)
}

// AddMandant is the wizard's write path: persist, start, re-plumb the mailroom.
func (l *Launcher) AddMandant(kind, title string) (*household.Mandant, error) {
	l.mu.Lock()
	m, err := l.hh.AddMandant(kind, title)
	if err != nil {
		l.mu.Unlock()
		return nil, err
	}
	if err := l.hh.Save(l.path); err != nil {
		l.mu.Unlock()
		return nil, err
	}
	mandant := *m
	l.mu.Unlock()
	l.startMandant(mandant)
	if err := l.plumbMailroom(); err != nil {
		l.log.Warn("mailroom plumbing after add failed", "error", err)
	}
	return &mandant, nil
}

func (l *Launcher) commonEnv(id, title, kind string, port int, dataDir string) map[string]string {
	env := map[string]string{
		"AITMATION_DATA_DIR":       dataDir,
		"AITMATION_PORT":           strconv.Itoa(port),
		"AITMATION_INSTANCE_ID":    id,
		"AITMATION_INSTANCE_TITLE": title,
		"AITMATION_INSTANCE_TYPE":  kind,
		"AITMATION_HOUSEHOLD_FILE": l.path,
		"AITMATION_LANGUAGE":       "de",
	}
	if l.cfg.CoreDir != "" {
		env["AITMATION_UI_DIR"] = filepath.Join(filepath.Dir(l.cfg.CoreDir), "ui", "dist")
	}
	if l.auth != nil {
		env["AITMATION_AUTH_SECRET"] = l.auth.SecretHex()
		env["AITMATION_LAUNCHER_URL"] = fmt.Sprintf("http://127.0.0.1:%d", l.hh.Launcher.Port)
	}
	return env
}

func (l *Launcher) startMandant(m household.Mandant) {
	dir := l.hh.MandantDir(l.cfg.DataDir, m)
	env := l.commonEnv(m.ID, m.Title, m.Type, m.Port, dir)
	env["AITMATION_TEMPLATES_DIR"] = filepath.Join(l.cfg.CoreDir, "templates")
	if l.cfg.AppsDir != "" {
		env["AITMATION_APPS_DIR"] = l.cfg.AppsDir
	}
	l.sup.Start(supervise.Spec{ID: m.ID, Command: l.cfg.CoreCmd, Dir: l.cfg.CoreDir, Env: env, Port: m.Port})
}

func (l *Launcher) startMailroom() {
	dir := l.hh.MailroomDir(l.cfg.DataDir)
	env := l.commonEnv("mailroom", l.hh.Mailroom.Title, "mailroom", l.hh.Mailroom.Port, dir)
	env["AITMATION_TEMPLATES_DIR"] = filepath.Join(l.cfg.CoreDir, "mailroom")
	l.sup.Start(supervise.Spec{ID: "mailroom", Command: l.cfg.CoreCmd, Dir: l.cfg.CoreDir, Env: env, Port: l.hh.Mailroom.Port})
}

// plumbMailroom writes the addressee table, asks the mailroom to reload,
// and — in the single-mandant case (docs/VERMITTLER.md) — grants the
// catch-all mandate once, so mail flows without a gate per document.
func (l *Launcher) plumbMailroom() error {
	l.mu.Lock()
	hh := l.hh
	dir := hh.MailroomDir(l.cfg.DataDir)
	toml := hh.AddresseesTOML(l.cfg.DataDir)
	single := ""
	if len(hh.Mandants) == 1 {
		single = hh.Mandants[0].ID
	}
	port := hh.Mailroom.Port
	l.mu.Unlock()

	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "addressees.toml"), []byte(toml), 0o640); err != nil {
		return err
	}
	if !supervise.WaitHealthy(l.ctx, port, 60*time.Second) {
		return errors.New("mailroom not healthy in time")
	}
	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	if resp, err := l.call(http.MethodPost, base+"/admin/reload", "application/x-www-form-urlencoded", nil); err == nil {
		resp.Body.Close()
	}
	if single != "" {
		return l.ensureCatchAll(base, single)
	}
	return nil
}

func (l *Launcher) call(method, url, contentType string, body []byte) (*http.Response, error) {
	req, err := http.NewRequest(method, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	for k, v := range l.serviceHeaders() {
		req.Header.Set(k, v)
	}
	return http.DefaultClient.Do(req)
}

// ensureCatchAll grants "everything -> the only mandant" unless a mandate
// already exists. Granting is recorded as the launcher's act in the
// mailroom's audit; a human can revoke it in the UI at any time.
func (l *Launcher) ensureCatchAll(base, target string) error {
	resp, err := l.call(http.MethodGet, base+"/api/v1/mandates", "", nil)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	var existing []map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&existing); err != nil {
		return err
	}
	for _, m := range existing {
		if m["revoked_at"] == nil {
			return nil
		}
	}
	body, _ := json.Marshal(map[string]any{
		"action": "forward", "target": target, "condition": map[string]string{},
		"actor": "launcher", "comment": "catch-all for the single mandant (docs/VERMITTLER.md)",
	})
	r, err := l.call(http.MethodPost, base+"/api/v1/mandates", "application/json", body)
	if err != nil {
		return err
	}
	r.Body.Close()
	return nil
}

// Run starts everything and serves the launcher API until cancelled.
func (l *Launcher) Run(ctx context.Context) error {
	l.ctx = ctx
	l.sup = supervise.New(ctx, l.log)
	for _, d := range []string{"mandants", "mailroom", "backups"} {
		if err := os.MkdirAll(filepath.Join(l.cfg.DataDir, d), 0o750); err != nil {
			return err
		}
	}
	// TODO phase 2: initial installation if no current release exists
	//   (release.FetchLatest / Verify / Unpack / SwitchCurrent, ADR-0003);
	//   backups before every update, rollback on red health (ADR-0013).
	l.startMailroom()
	for _, m := range l.hh.Mandants {
		l.startMandant(m)
	}
	go func() {
		if err := l.plumbMailroom(); err != nil {
			l.log.Warn("mailroom plumbing failed", "error", err)
		}
		if l.openBrowser {
			openInBrowser(l.ShellURL())
		}
	}()

	srv := &http.Server{
		Addr:              fmt.Sprintf("127.0.0.1:%d", l.hh.Launcher.Port),
		Handler:           api.Handler(l),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()
	l.log.Info("launcher started", "version", version, "datadir", l.cfg.DataDir, "household", l.path,
		"mandants", len(l.hh.Mandants), "status", "http://"+srv.Addr+"/", "shell", l.ShellURL())
	err := srv.ListenAndServe()
	l.sup.Wait()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// absPath makes a path absolute; empty stays empty, failures keep the input.
func absPath(p string) string {
	if p == "" {
		return p
	}
	if abs, err := filepath.Abs(p); err == nil {
		return abs
	}
	return p
}

type multiFlag []string

func (m *multiFlag) String() string     { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error { *m = append(*m, v); return nil }

func main() {
	log := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	cfg := defaultConfig()
	sub := "run"
	args := os.Args[1:]
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		sub, args = args[0], args[1:]
	}
	switch sub {
	case "init":
		fs := flag.NewFlagSet("init", flag.ExitOnError)
		fs.StringVar(&cfg.DataDir, "data-dir", cfg.DataDir, "data directory")
		admin := fs.String("admin", os.Getenv("USER"), "name of the administering person")
		password := fs.String("admin-password", os.Getenv("AITMATION_ADMIN_PASSWORD"), "admin password (or AITMATION_ADMIN_PASSWORD; generated when empty)")
		private := fs.Bool("private", false, "add a private mandant")
		var companies multiFlag
		fs.Var(&companies, "company", "add a company mandant with this title (repeatable)")
		_ = fs.Parse(args)
		if err := runInit(cfg, *admin, *password, *private, companies); err != nil {
			log.Error("init failed", "error", err)
			os.Exit(1)
		}
	case "run":
		fs := flag.NewFlagSet("run", flag.ExitOnError)
		fs.StringVar(&cfg.DataDir, "data-dir", cfg.DataDir, "data directory")
		fs.StringVar(&cfg.CoreDir, "core-dir", cfg.CoreDir, "directory containing the core (checkout or release)")
		fs.StringVar(&cfg.AppsDir, "apps-dir", cfg.AppsDir, "catalog source (apps/)")
		coreCmd := fs.String("core-cmd", strings.Join(cfg.CoreCmd, " "), "command that starts a core")
		autoInit := fs.Bool("auto-init", false, "no household yet: create one (admin = this user, one private mandant) instead of failing")
		openBrowser := fs.Bool("open-browser", false, "open the shell in the default browser once the mailroom is healthy")
		_ = fs.Parse(args)
		cfg.CoreCmd = strings.Fields(*coreCmd)
		if cfg.CoreDir == "" {
			log.Error("run needs -core-dir (or AITMATION_CORE_DIR)")
			os.Exit(2)
		}
		// Cores start with CoreDir as their working directory, so every path
		// handed to them - and the interpreter path itself - must be absolute;
		// a relative `-core-dir core` would otherwise resolve to core/core/...
		cfg.DataDir, cfg.CoreDir, cfg.AppsDir = absPath(cfg.DataDir), absPath(cfg.CoreDir), absPath(cfg.AppsDir)
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		if *coreCmd == strings.Join(defaultConfig().CoreCmd, " ") {
			cfg.CoreCmd = resolveCoreCmd(ctx, cfg.CoreDir, cfg.DataDir, cfg.CoreCmd, log)
		}
		path := filepath.Join(cfg.DataDir, "household.toml")
		if _, err := os.Stat(path); err != nil && *autoInit {
			// Double-click start of a bundle (principle 4): a private household
			// for the person at the keyboard, password shown once on this console.
			admin := strings.ToLower(firstNonEmpty(os.Getenv("USERNAME"), os.Getenv("USER"), "admin"))
			if auth.ValidName(admin) != nil {
				admin = "admin"
			}
			if err := runInit(cfg, admin, os.Getenv("AITMATION_ADMIN_PASSWORD"), true, nil); err != nil {
				log.Error("auto-init failed", "error", err)
				os.Exit(1)
			}
		}
		hh, err := household.Load(path)
		if err != nil {
			log.Error("no usable household - run `aitmation-launcher init` first", "path", path, "error", err)
			os.Exit(1)
		}
		store, err := auth.Open(cfg.DataDir)
		if err != nil {
			log.Error("identity store unreadable", "error", err)
			os.Exit(1)
		}
		if store == nil {
			log.Warn("no users - authentication is off; `aitmation-launcher user add` turns it on (ADR-0025)")
		}
		l := &Launcher{cfg: cfg, log: log, path: path, hh: hh, auth: store, openBrowser: *openBrowser}
		if err := l.Run(ctx); err != nil {
			log.Error("launcher stopped", "error", err)
			os.Exit(1)
		}
	case "user":
		if err := runUser(cfg, args); err != nil {
			log.Error("user command failed", "error", err)
			os.Exit(1)
		}
	case "token":
		fs := flag.NewFlagSet("token", flag.ExitOnError)
		fs.StringVar(&cfg.DataDir, "data-dir", cfg.DataDir, "data directory")
		user := fs.String("user", "", "user the token is issued for")
		days := fs.Int("days", 365, "validity in days")
		_ = fs.Parse(args)
		if err := runToken(cfg, *user, *days); err != nil {
			log.Error("token failed", "error", err)
			os.Exit(1)
		}
	default:
		fmt.Fprintln(os.Stderr, "usage: aitmation-launcher [init|run|user|token] [flags]")
		os.Exit(2)
	}
}

// runUser manages the household's people: add | remove | passwd.
func runUser(cfg Config, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: aitmation-launcher user add|remove|passwd -name N [-password P] [-role admin|member] [-instances a,b]")
	}
	verb, args := args[0], args[1:]
	fs := flag.NewFlagSet("user "+verb, flag.ExitOnError)
	fs.StringVar(&cfg.DataDir, "data-dir", cfg.DataDir, "data directory")
	name := fs.String("name", "", "user name")
	password := fs.String("password", os.Getenv("AITMATION_PASSWORD"), "password (or AITMATION_PASSWORD; generated when empty)")
	role := fs.String("role", auth.RoleMember, "admin or member")
	instances := fs.String("instances", "", "comma-separated mandant ids a member may work in")
	_ = fs.Parse(args)
	if *name == "" {
		return errors.New("-name is required")
	}
	store, err := auth.OpenOrCreate(cfg.DataDir)
	if err != nil {
		return err
	}
	switch verb {
	case "add":
		pw, generated := *password, false
		if pw == "" {
			pw, generated = randomPassword(), true
		}
		var list []string
		for _, id := range strings.Split(*instances, ",") {
			if id = strings.TrimSpace(id); id != "" {
				list = append(list, id)
			}
		}
		u, err := store.Add(*name, pw, *role, list)
		if err != nil {
			return err
		}
		fmt.Printf("user %s (%s, instances %s) added; authentication is on for this household\n", u.Name, u.Role, strings.Join(u.Instances, ","))
		if generated {
			fmt.Printf("password (shown once): %s\n", pw)
		}
		return nil
	case "remove":
		return store.Remove(*name)
	case "passwd":
		pw, generated := *password, false
		if pw == "" {
			pw, generated = randomPassword(), true
		}
		if err := store.SetPassword(*name, pw); err != nil {
			return err
		}
		if generated {
			fmt.Printf("new password for %s (shown once): %s\n", *name, pw)
		}
		return nil
	}
	return fmt.Errorf("unknown verb %q (add|remove|passwd)", verb)
}

// runToken mints a long-lived bearer token, e.g. for the MCP channel's
// AITMATION_TOKEN. It is a credential: shown once, never logged.
func runToken(cfg Config, user string, days int) error {
	if user == "" {
		return errors.New("-user is required")
	}
	store, err := auth.Open(cfg.DataDir)
	if err != nil {
		return err
	}
	if store == nil {
		return errors.New("this household has no users; authentication is off and no token is needed")
	}
	var found *auth.User
	for _, u := range store.Users() {
		if u.Name == user {
			found = &u
			break
		}
	}
	if found == nil {
		return fmt.Errorf("user %q not found", user)
	}
	fmt.Println(store.Token(*found, time.Duration(days)*24*time.Hour))
	return nil
}

func randomPassword() string {
	const alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	for i := range b {
		b[i] = alphabet[int(b[i])%len(alphabet)]
	}
	return string(b)
}

// runInit writes household.toml for a fresh installation. The product
// shell's wizard adds further mandants later through the launcher API.
func runInit(cfg Config, admin, password string, private bool, companies []string) error {
	if err := os.MkdirAll(cfg.DataDir, 0o750); err != nil {
		return err
	}
	path := filepath.Join(cfg.DataDir, "household.toml")
	if _, err := os.Stat(path); err == nil {
		return fmt.Errorf("%s exists already - add mandants through the shell or edit the file", path)
	}
	if admin == "" {
		admin = "admin"
	}
	if err := auth.ValidName(admin); err != nil {
		return fmt.Errorf("%w - pass -admin <name>", err)
	}
	if password != "" && len(password) < 8 {
		return errors.New("-admin-password must have at least 8 characters")
	}
	hh := household.New(admin)
	if private {
		if _, err := hh.AddMandant(household.TypePrivate, ""); err != nil {
			return err
		}
	}
	for _, c := range companies {
		if _, err := hh.AddMandant(household.TypeCompany, c); err != nil {
			return err
		}
	}
	if err := hh.Save(path); err != nil {
		return err
	}
	// The admin is the first user; from here on the household requires a
	// login (ADR-0025). A generated password is shown exactly once.
	store, err := auth.Create(cfg.DataDir)
	if err != nil {
		return err
	}
	generated := false
	if password == "" {
		password, generated = randomPassword(), true
	}
	if _, err := store.Add(admin, password, auth.RoleAdmin, nil); err != nil {
		return err
	}
	fmt.Printf("wrote %s (%d mandant(s)); next: aitmation-launcher run -data-dir %s -core-dir <core>\n",
		path, len(hh.Mandants), cfg.DataDir)
	if generated {
		fmt.Printf("admin user %q, password (shown once): %s\n", strings.ToLower(admin), password)
	} else {
		fmt.Printf("admin user %q created\n", strings.ToLower(admin))
	}
	return nil
}
