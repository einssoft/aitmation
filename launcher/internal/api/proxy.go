// Reverse proxy: the launcher is the household's single origin (ADR-0032).
//
//	/app/…       -> mailroom  (the shell is served by the mailroom instance)
//	/api/v1/…    -> mailroom  (the shell addresses "self" relatively)
//	/m/<id>/…    -> that instance, prefix stripped
//
// Every forwarded request carries X-Aitmation-Proxy so the core answers
// with origin-relative member URLs. The launcher forwards, it does not
// understand (ADR-0002, ADR-0020).
package api

import (
	"fmt"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
)

// ProxyHeader marks requests that reached a core through the launcher.
const ProxyHeader = "X-Aitmation-Proxy"

// instancePort resolves a member id to its loopback port; 0 = unknown.
func instancePort(r Runner, id string) int {
	h := r.Household()
	if id == "mailroom" {
		return h.Mailroom.Port
	}
	for _, m := range h.Mandants {
		if m.ID == id {
			return m.Port
		}
	}
	return 0
}

// proxyTo forwards the request to 127.0.0.1:<port> with the given path.
func proxyTo(w http.ResponseWriter, req *http.Request, port int, path string) {
	target := &url.URL{Scheme: "http", Host: fmt.Sprintf("127.0.0.1:%d", port)}
	rp := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(target)
			pr.Out.URL.Path = path
			pr.Out.URL.RawPath = ""
			pr.Out.Host = target.Host
			pr.SetXForwarded()
			pr.Out.Header.Set(ProxyHeader, "1")
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			http.Error(w, "instance not reachable: "+err.Error(), http.StatusBadGateway)
		},
	}
	rp.ServeHTTP(w, req)
}

// mailroomProxy serves /app/… and /api/v1/… from the mailroom.
func mailroomProxy(r Runner) http.HandlerFunc {
	return func(w http.ResponseWriter, req *http.Request) {
		if !cleanPath(req.URL.Path) {
			http.NotFound(w, req)
			return
		}
		proxyTo(w, req, r.Household().Mailroom.Port, req.URL.Path)
	}
}

// memberProxy serves /m/<id>/… from that member with the prefix removed.
func memberProxy(r Runner) http.HandlerFunc {
	return func(w http.ResponseWriter, req *http.Request) {
		if !cleanPath(req.URL.Path) {
			http.NotFound(w, req)
			return
		}
		rest := strings.TrimPrefix(req.URL.Path, "/m/")
		id, path, _ := strings.Cut(rest, "/")
		port := instancePort(r, id)
		if id == "" || port == 0 {
			http.NotFound(w, req)
			return
		}
		proxyTo(w, req, port, "/"+path)
	}
}

// cleanPath refuses traversal and empty segments the mux would not see.
func cleanPath(p string) bool {
	for _, seg := range strings.Split(p, "/") {
		if seg == ".." || seg == "." {
			return false
		}
	}
	return !strings.Contains(p, "//")
}
