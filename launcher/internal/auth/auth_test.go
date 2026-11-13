package auth

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestOpenWithoutUsersMeansAuthOff(t *testing.T) {
	s, err := Open(t.TempDir())
	if err != nil || s != nil {
		t.Fatalf("expected nil store, got %v %v", s, err)
	}
}

func TestAddLoginTokenRoundTrip(t *testing.T) {
	dir := t.TempDir()
	s, err := Create(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Add("Alex", "geheim123", RoleAdmin, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Add("anna", "geheim123", RoleMember, []string{"privat"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Add("anna", "geheim123", RoleMember, []string{"privat"}); err == nil {
		t.Fatal("duplicate accepted")
	}
	if _, err := s.Add("bob", "kurz", RoleMember, []string{"privat"}); err == nil {
		t.Fatal("short password accepted")
	}
	info, err := os.Stat(filepath.Join(dir, UsersFile))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0o077 != 0 {
		t.Fatalf("users file too open: %v", info.Mode())
	}

	u, err := s.Login("alex", "geheim123")
	if err != nil || u.Role != RoleAdmin || u.Instances[0] != Everything {
		t.Fatalf("login: %v %+v", err, u)
	}
	if _, err := s.Login("alex", "falsch"); err == nil {
		t.Fatal("wrong password accepted")
	}
	if _, err := s.Login("niemand", "geheim123"); err == nil {
		t.Fatal("unknown user accepted")
	}

	tok := s.Token(u, time.Minute)
	p, err := s.Verify(tok)
	if err != nil || p.Sub != "alex" || !p.IsAdmin() {
		t.Fatalf("verify: %v %+v", err, p)
	}
	if _, err := Verify([]byte("other-secret-other-secret-other-"), tok); err == nil {
		t.Fatal("foreign secret verified")
	}
	if _, err := s.Verify(tok[:len(tok)-2] + "xx"); err == nil {
		t.Fatal("tampered signature verified")
	}
	expired := Sign(s.secret, Principal{Sub: "alex", Role: RoleAdmin, Exp: time.Now().Add(-time.Second).Unix()})
	if _, err := s.Verify(expired); err == nil {
		t.Fatal("expired token verified")
	}

	// Reopen: same secret, same users.
	again, err := Open(dir)
	if err != nil || again == nil {
		t.Fatal(err)
	}
	if again.SecretHex() != s.SecretHex() || again.Count() != 2 {
		t.Fatal("store did not persist")
	}
	if err := again.Remove("alex"); err == nil {
		t.Fatal("last admin removed")
	}
	if err := again.Remove("anna"); err != nil {
		t.Fatal(err)
	}
	if err := again.SetPassword("alex", "neuesgeheim"); err != nil {
		t.Fatal(err)
	}
	if _, err := again.Login("alex", "neuesgeheim"); err != nil {
		t.Fatal(err)
	}
	for _, u := range again.Users() {
		if u.Hash != "" || u.Salt != "" {
			t.Fatal("Users() leaks secrets")
		}
	}
}
