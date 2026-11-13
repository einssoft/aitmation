// Package api is the launcher's small HTTP surface: a framework-free status
// page (ADR-0019 keeps the launcher UI plain), the household endpoints the
// product shell's setup wizard talks to (ADR-0024), and the household's
// login (ADR-0025), and — as the household's single origin (ADR-0032) — the
// reverse proxy to the shell and the instances. Binds loopback only; what
// exposes it (tailscale serve, Coolify) is the operator's choice.
package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"net/http"
	"strings"
	"time"

	"github.com/aitmation/aitmation/launcher/internal/auth"
	"github.com/aitmation/aitmation/launcher/internal/household"
	"github.com/aitmation/aitmation/launcher/internal/supervise"
)

// Runner is what the API needs from the launcher: read the household,
// add a mandant (persist + start), report instance status, and reach the
// identity store (nil when the household has no users yet).
type Runner interface {
	Household() *household.Household
	AddMandant(kind, title string) (*household.Mandant, error)
	Statuses() []supervise.Status
	ShellURL() string
	Auth() *auth.Store
}

func Handler(r Runner) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", statusPage(r))
	// One origin for the household (ADR-0032): shell and instances via the launcher.
	mux.HandleFunc("/app", mailroomProxy(r))
	mux.HandleFunc("/app/", mailroomProxy(r))
	mux.HandleFunc("/api/v1/", mailroomProxy(r))
	mux.HandleFunc("/m/", memberProxy(r))
	mux.HandleFunc("GET /api/household", func(w http.ResponseWriter, req *http.Request) {
		writeJSON(w, map[string]any{
			"household":     r.Household(),
			"instances":     r.Statuses(),
			"shell_url":     r.ShellURL(),
			"auth_required": r.Auth() != nil,
		})
	})
	mux.HandleFunc("POST /api/household/mandants", requireAdmin(r, func(w http.ResponseWriter, req *http.Request, _ *auth.Principal) {
		var body struct {
			Type  string `json:"type"`
			Title string `json:"title"`
		}
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		m, err := r.AddMandant(body.Type, body.Title)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		writeJSON(w, m)
	}))

	// -- identity (ADR-0025) ------------------------------------------------

	mux.HandleFunc("POST /api/auth/login", func(w http.ResponseWriter, req *http.Request) {
		store := r.Auth()
		if store == nil {
			http.Error(w, "this household has no users; authentication is off", http.StatusNotFound)
			return
		}
		var body struct {
			Name     string `json:"name"`
			Password string `json:"password"`
		}
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		u, err := store.Login(body.Name, body.Password)
		if err != nil {
			time.Sleep(300 * time.Millisecond)
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		token := store.Token(u, auth.SessionTTL)
		http.SetCookie(w, sessionCookie(token, auth.SessionTTL, viaTLS(req)))
		writeJSON(w, map[string]any{"user": u, "token": token})
	})
	mux.HandleFunc("POST /api/auth/logout", func(w http.ResponseWriter, req *http.Request) {
		http.SetCookie(w, sessionCookie("", -time.Hour, viaTLS(req)))
		writeJSON(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("GET /api/auth/me", func(w http.ResponseWriter, req *http.Request) {
		store := r.Auth()
		if store == nil {
			writeJSON(w, map[string]any{"auth_required": false, "principal": nil})
			return
		}
		p, err := principalOf(store, req)
		if err != nil {
			w.WriteHeader(http.StatusUnauthorized)
			writeJSON(w, map[string]any{"auth_required": true, "principal": nil, "error": err.Error()})
			return
		}
		writeJSON(w, map[string]any{"auth_required": true, "principal": p})
	})
	mux.HandleFunc("POST /api/auth/password", requireUser(r, func(w http.ResponseWriter, req *http.Request, p *auth.Principal) {
		var body struct {
			Current string `json:"current"`
			New     string `json:"new"`
		}
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		if _, err := r.Auth().Login(p.Sub, body.Current); err != nil {
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		if err := r.Auth().SetPassword(p.Sub, body.New); err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		writeJSON(w, map[string]any{"ok": true})
	}))

	mux.HandleFunc("GET /api/household/users", requireAdmin(r, func(w http.ResponseWriter, req *http.Request, _ *auth.Principal) {
		writeJSON(w, r.Auth().Users())
	}))
	mux.HandleFunc("POST /api/household/users", requireAdmin(r, func(w http.ResponseWriter, req *http.Request, _ *auth.Principal) {
		var body struct {
			Name      string   `json:"name"`
			Password  string   `json:"password"`
			Role      string   `json:"role"`
			Instances []string `json:"instances"`
		}
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		if body.Role == "" {
			body.Role = auth.RoleMember
		}
		u, err := r.Auth().Add(body.Name, body.Password, body.Role, body.Instances)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		writeJSON(w, u)
	}))
	mux.HandleFunc("POST /api/household/users/{name}/remove", requireAdmin(r, func(w http.ResponseWriter, req *http.Request, p *auth.Principal) {
		name := req.PathValue("name")
		if name == p.Sub {
			http.Error(w, "you cannot remove yourself", http.StatusUnprocessableEntity)
			return
		}
		if err := r.Auth().Remove(name); err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		writeJSON(w, map[string]any{"ok": true})
	}))
	return withCORS(mux)
}

type handler func(http.ResponseWriter, *http.Request, *auth.Principal)

// requireUser passes when authentication is off, or the request carries a
// valid token.
func requireUser(r Runner, next handler) http.HandlerFunc {
	return func(w http.ResponseWriter, req *http.Request) {
		store := r.Auth()
		if store == nil {
			next(w, req, nil)
			return
		}
		p, err := principalOf(store, req)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		next(w, req, &p)
	}
}

// requireAdmin: like requireUser, and the principal must be an admin. With
// authentication off there is no admin either - management then needs the
// command line, which is the honest state of a household without users.
func requireAdmin(r Runner, next handler) http.HandlerFunc {
	return func(w http.ResponseWriter, req *http.Request) {
		store := r.Auth()
		if store == nil {
			if strings.HasPrefix(req.URL.Path, "/api/household/users") {
				http.Error(w, "this household has no users; use `aitmation-launcher user add`", http.StatusNotFound)
				return
			}
			next(w, req, nil)
			return
		}
		p, err := principalOf(store, req)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		if !p.IsAdmin() {
			http.Error(w, "admin only", http.StatusForbidden)
			return
		}
		next(w, req, &p)
	}
}

// principalOf reads the session cookie or a bearer token.
func principalOf(store *auth.Store, req *http.Request) (auth.Principal, error) {
	token := ""
	if h := req.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
		token = strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
	} else if c, err := req.Cookie(auth.CookieName); err == nil {
		token = c.Value
	}
	if token == "" {
		return auth.Principal{}, errors.New("not signed in")
	}
	return store.Verify(token)
}

// viaTLS: the request came through a TLS-terminating proxy (ADR-0032 §4).
func viaTLS(req *http.Request) bool {
	return req.TLS != nil || strings.EqualFold(req.Header.Get("X-Forwarded-Proto"), "https")
}

func sessionCookie(token string, ttl time.Duration, secure bool) *http.Cookie {
	c := &http.Cookie{
		Name:     auth.CookieName,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
	}
	if ttl < 0 {
		c.MaxAge = -1
	} else {
		c.MaxAge = int(ttl.Seconds())
	}
	return c
}

// withCORS allows the product shell (served by any core instance on this
// machine) to call the launcher with its session cookie; nothing
// off-loopback is a peer.
func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		origin := req.Header.Get("Origin")
		if strings.HasPrefix(origin, "http://127.0.0.1") || strings.HasPrefix(origin, "http://localhost") {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Access-Control-Allow-Credentials", "true")
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "content-type, authorization")
			w.Header().Set("Vary", "Origin")
		}
		if req.Method == http.MethodOptions && strings.HasPrefix(req.URL.Path, "/api/") {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, req)
	})
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func statusPage(r Runner) http.HandlerFunc {
	return func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/" {
			http.NotFound(w, req)
			return
		}
		h := r.Household()
		var rows strings.Builder
		for _, st := range r.Statuses() {
			state := "aus"
			if st.Running && st.Healthy {
				state = "läuft"
			} else if st.Running {
				state = "startet"
			}
			fmt.Fprintf(&rows, "<tr><td><code>%s</code></td><td>%d</td><td>%s</td><td>%d</td><td>%s</td></tr>",
				html.EscapeString(st.ID), st.Port, state, st.Restarts, html.EscapeString(st.LastError))
		}
		login := "Anmeldung: aus (keine Benutzer angelegt)"
		if r.Auth() != nil {
			login = fmt.Sprintf("Anmeldung: an (%d Benutzer)", r.Auth().Count())
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<!doctype html><html lang="de"><head><meta charset="utf-8"><title>aitmation Launcher</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:56rem;line-height:1.5}table{border-collapse:collapse}td,th{padding:.3rem .8rem;border-bottom:1px solid #ddd;text-align:left}code{background:#f3f3f3;padding:0 .3em}</style>
</head><body><h1>aitmation Launcher</h1>
<p>Haushalt von <strong>%s</strong> · %d Mandant(en) · %s · <a href="/app/">Oberfläche öffnen</a></p>
<table><tr><th>Instanz</th><th>Port</th><th>Zustand</th><th>Neustarts</th><th>Letzter Fehler</th></tr>%s</table>
<p><small>Diese Seite ist bewusst schlicht: der Launcher ist ein Lebenszyklus-Manager, kein Produkt (ADR-0002, ADR-0020).</small></p>
<p><small>Bedienung von anderen Geräten: alles läuft über diesen einen Ursprung (<code>/app/</code>, <code>/m/&lt;mandant&gt;/</code>, ADR-0032) — nach aussen nur hinter TLS und mit Anmeldung.</small></p>
</body></html>`, html.EscapeString(h.Admin), len(h.Mandants), login, rows.String())
	}
}
