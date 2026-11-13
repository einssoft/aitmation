package api

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/aitmation/aitmation/launcher/internal/auth"
	"github.com/aitmation/aitmation/launcher/internal/household"
	"github.com/aitmation/aitmation/launcher/internal/supervise"
)

type fakeRunner struct{ hh *household.Household }

func (f *fakeRunner) Household() *household.Household { return f.hh }
func (f *fakeRunner) AddMandant(kind, title string) (*household.Mandant, error) {
	return f.hh.AddMandant(kind, title)
}
func (f *fakeRunner) Statuses() []supervise.Status { return nil }
func (f *fakeRunner) ShellURL() string             { return "http://127.0.0.1:8400/app/" }
func (f *fakeRunner) Auth() *auth.Store            { return nil }

// echoServer answers on a loopback port with what it received.
func echoServer(t *testing.T, name string) (port int, close func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "%s %s %s proxy=%s host=%s proto=%s", name, r.Method, r.URL.RequestURI(),
			r.Header.Get(ProxyHeader), r.Header.Get("X-Forwarded-Host"), r.Header.Get("X-Forwarded-Proto"))
	})}
	go func() { _ = srv.Serve(ln) }()
	return ln.Addr().(*net.TCPAddr).Port, func() { _ = srv.Close() }
}

func TestProxyRoutesToMailroomAndMembers(t *testing.T) {
	mailroomPort, closeM := echoServer(t, "mailroom")
	defer closeM()
	privatePort, closeP := echoServer(t, "private")
	defer closeP()

	hh := household.New("alex")
	hh.Mailroom.Port = mailroomPort
	hh.Mandants = []household.Mandant{{ID: "private", Title: "Privat", Type: "private", Port: privatePort}}
	h := Handler(&fakeRunner{hh: hh})
	front := httptest.NewServer(h)
	defer front.Close()

	get := func(path string) (int, string) {
		resp, err := http.Get(front.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(b)
	}

	cases := map[string]string{
		"/app/":                       "mailroom GET /app/ proxy=1",
		"/app/pendenzen":              "mailroom GET /app/pendenzen proxy=1",
		"/api/v1/household":           "mailroom GET /api/v1/household proxy=1",
		"/m/mailroom/api/v1/gates":    "mailroom GET /api/v1/gates proxy=1",
		"/m/private/api/v1/cases?x=1": "private GET /api/v1/cases?x=1 proxy=1",
		"/m/private/":                 "private GET / proxy=1",
	}
	for path, want := range cases {
		code, body := get(path)
		if code != 200 || !strings.HasPrefix(body, want) {
			t.Errorf("%s: got %d %q, want prefix %q", path, code, body, want)
		}
		if !strings.Contains(body, "host=127.0.0.1:") {
			t.Errorf("%s: X-Forwarded-Host missing: %q", path, body)
		}
	}
	for _, path := range []string{"/m/unknown/api/v1/gates", "/m/", "/m/private/%2e%2e/mailroom/x"} {
		if code, _ := get(path); code != 404 {
			t.Errorf("%s: want 404, got %d", path, code)
		}
	}
	// The launcher's own API is untouched.
	if code, body := get("/api/household"); code != 200 || !strings.Contains(body, `"household"`) {
		t.Errorf("/api/household: got %d %q", code, body)
	}
	if code, body := get("/"); code != 200 || !strings.Contains(body, `href="/app/"`) {
		t.Errorf("status page should link the shell relatively: %d %q", code, body)
	}
}

func TestProxyUnreachableInstanceIs502(t *testing.T) {
	hh := household.New("alex")
	hh.Mailroom.Port = 1 // nobody listens here
	front := httptest.NewServer(Handler(&fakeRunner{hh: hh}))
	defer front.Close()
	resp, err := http.Get(front.URL + "/app/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("want 502, got %d", resp.StatusCode)
	}
}

func TestSessionCookieSecureBehindTLS(t *testing.T) {
	plain := httptest.NewRequest("POST", "/api/auth/login", nil)
	if viaTLS(plain) {
		t.Error("loopback http must not be secure")
	}
	fwd := httptest.NewRequest("POST", "/api/auth/login", nil)
	fwd.Header.Set("X-Forwarded-Proto", "https")
	if !viaTLS(fwd) {
		t.Error("X-Forwarded-Proto https must be secure")
	}
	if c := sessionCookie("t", 60, true); !c.Secure || !c.HttpOnly {
		t.Errorf("cookie flags: %+v", c)
	}
}
