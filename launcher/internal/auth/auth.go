// Package auth is the household's identity store and token issuer
// (ADR-0025). The launcher owns the users file and the household secret;
// every core instance only verifies the tokens minted here. Nothing in
// this package talks to the network - handlers live in internal/api.
package auth

import (
	"crypto/hmac"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	UsersFile   = "users.json"
	SecretFile  = "auth-secret"
	CookieName  = "aitmation_session"
	SessionTTL  = 12 * time.Hour
	RoleAdmin   = "admin"
	RoleMember  = "member"
	Everything  = "*"
	pbkdfRounds = 600_000
	tokenPrefix = "v1"
)

var nameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// ValidName reports whether a user name is acceptable (lowercased first).
func ValidName(name string) error {
	name = strings.ToLower(strings.TrimSpace(name))
	if !nameRe.MatchString(name) {
		return fmt.Errorf("user name %q: use lowercase letters, digits, . _ - (no spaces)", name)
	}
	return nil
}

// User is one person of the household. Instances lists the mandant ids the
// person may work in; "*" means all (admins always see everything).
type User struct {
	Name      string   `json:"name"`
	Role      string   `json:"role"`
	Instances []string `json:"instances"`
	Salt      string   `json:"salt"`
	Hash      string   `json:"hash"`
	Rounds    int      `json:"rounds"`
	CreatedAt string   `json:"created_at"`
}

// Principal is what a verified token says about its bearer.
type Principal struct {
	Sub       string   `json:"sub"`
	Role      string   `json:"role"`
	Instances []string `json:"instances"`
	Exp       int64    `json:"exp"`
	Iat       int64    `json:"iat"`
}

func (p Principal) IsAdmin() bool { return p.Role == RoleAdmin }

// Store is the users file plus the secret, guarded for concurrent handlers.
type Store struct {
	dir    string
	mu     sync.Mutex
	users  []User
	secret []byte
}

// Open loads the store if the users file exists; a missing file means
// authentication is off for this household (ADR-0025 §7).
func Open(dataDir string) (*Store, error) {
	s := &Store{dir: dataDir}
	raw, err := os.ReadFile(filepath.Join(dataDir, UsersFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var file struct {
		Users []User `json:"users"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		return nil, fmt.Errorf("%s: %w", UsersFile, err)
	}
	s.users = file.Users
	if s.secret, err = loadOrCreateSecret(dataDir); err != nil {
		return nil, err
	}
	return s, nil
}

// Create makes a store for a household that has none yet.
func Create(dataDir string) (*Store, error) {
	if err := os.MkdirAll(dataDir, 0o750); err != nil {
		return nil, err
	}
	secret, err := loadOrCreateSecret(dataDir)
	if err != nil {
		return nil, err
	}
	return &Store{dir: dataDir, secret: secret}, nil
}

// OpenOrCreate is what `user add` needs: an existing store or a fresh one.
func OpenOrCreate(dataDir string) (*Store, error) {
	s, err := Open(dataDir)
	if err != nil || s != nil {
		return s, err
	}
	return Create(dataDir)
}

func loadOrCreateSecret(dataDir string) ([]byte, error) {
	path := filepath.Join(dataDir, SecretFile)
	raw, err := os.ReadFile(path)
	if err == nil {
		secret, decErr := hex.DecodeString(strings.TrimSpace(string(raw)))
		if decErr != nil || len(secret) < 32 {
			return nil, fmt.Errorf("%s: not a valid secret", path)
		}
		return secret, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	secret := make([]byte, 32)
	if _, err := rand.Read(secret); err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, []byte(hex.EncodeToString(secret)+"\n"), 0o600); err != nil {
		return nil, err
	}
	return secret, nil
}

// SecretHex is what the launcher hands each core as AITMATION_AUTH_SECRET.
func (s *Store) SecretHex() string { return hex.EncodeToString(s.secret) }

func (s *Store) save() error {
	sort.Slice(s.users, func(i, j int) bool { return s.users[i].Name < s.users[j].Name })
	raw, err := json.MarshalIndent(struct {
		Users []User `json:"users"`
	}{s.users}, "", "  ")
	if err != nil {
		return err
	}
	path := filepath.Join(s.dir, UsersFile)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(raw, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Users returns the list without secrets, for the admin UI.
func (s *Store) Users() []User {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]User, 0, len(s.users))
	for _, u := range s.users {
		u.Salt, u.Hash, u.Rounds = "", "", 0
		out = append(out, u)
	}
	return out
}

func (s *Store) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.users)
}

// Add creates a user. Instances are normalized; admins get "*".
func (s *Store) Add(name, password, role string, instances []string) (User, error) {
	if err := ValidName(name); err != nil {
		return User{}, err
	}
	name = strings.ToLower(strings.TrimSpace(name))
	if len(password) < 8 {
		return User{}, errors.New("password must have at least 8 characters")
	}
	if role != RoleAdmin && role != RoleMember {
		return User{}, fmt.Errorf("role %q: admin or member", role)
	}
	if role == RoleAdmin {
		instances = []string{Everything}
	}
	if len(instances) == 0 {
		return User{}, errors.New("a member needs at least one instance")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, u := range s.users {
		if u.Name == name {
			return User{}, fmt.Errorf("user %q exists already", name)
		}
	}
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return User{}, err
	}
	u := User{
		Name:      name,
		Role:      role,
		Instances: instances,
		Salt:      hex.EncodeToString(salt),
		Hash:      hex.EncodeToString(derive(password, salt, pbkdfRounds)),
		Rounds:    pbkdfRounds,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	s.users = append(s.users, u)
	if err := s.save(); err != nil {
		return User{}, err
	}
	u.Salt, u.Hash, u.Rounds = "", "", 0
	return u, nil
}

// Remove deletes a user; the last admin cannot be removed.
func (s *Store) Remove(name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	admins := 0
	idx := -1
	for i, u := range s.users {
		if u.Role == RoleAdmin {
			admins++
		}
		if u.Name == name {
			idx = i
		}
	}
	if idx < 0 {
		return fmt.Errorf("user %q not found", name)
	}
	if s.users[idx].Role == RoleAdmin && admins == 1 {
		return errors.New("the last admin cannot be removed")
	}
	s.users = append(s.users[:idx], s.users[idx+1:]...)
	return s.save()
}

// SetPassword replaces a user's password.
func (s *Store) SetPassword(name, password string) error {
	if len(password) < 8 {
		return errors.New("password must have at least 8 characters")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.users {
		if s.users[i].Name != name {
			continue
		}
		salt := make([]byte, 16)
		if _, err := rand.Read(salt); err != nil {
			return err
		}
		s.users[i].Salt = hex.EncodeToString(salt)
		s.users[i].Hash = hex.EncodeToString(derive(password, salt, pbkdfRounds))
		s.users[i].Rounds = pbkdfRounds
		return s.save()
	}
	return fmt.Errorf("user %q not found", name)
}

// Login checks the password and returns the user, or an error that never
// says which half was wrong.
func (s *Store) Login(name, password string) (User, error) {
	name = strings.ToLower(strings.TrimSpace(name))
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, u := range s.users {
		if u.Name != name {
			continue
		}
		salt, err := hex.DecodeString(u.Salt)
		if err != nil {
			break
		}
		want, err := hex.DecodeString(u.Hash)
		if err != nil {
			break
		}
		if subtle.ConstantTimeCompare(derive(password, salt, u.Rounds), want) == 1 {
			u.Salt, u.Hash, u.Rounds = "", "", 0
			return u, nil
		}
		break
	}
	// Burn the same time on a miss so timing does not reveal user names.
	derive(password, []byte("aitmation-miss--"), pbkdfRounds)
	return User{}, errors.New("unknown user or wrong password")
}

func derive(password string, salt []byte, rounds int) []byte {
	key, err := pbkdf2.Key(sha256.New, password, salt, rounds, 32)
	if err != nil {
		panic(err) // parameters are constants; only a broken hash could fail
	}
	return key
}

// Token mints a signed token for a user, valid for ttl.
func (s *Store) Token(u User, ttl time.Duration) string {
	return Sign(s.secret, Principal{
		Sub:       u.Name,
		Role:      u.Role,
		Instances: u.Instances,
		Iat:       time.Now().Unix(),
		Exp:       time.Now().Add(ttl).Unix(),
	})
}

// ServiceToken is the launcher's own identity towards the cores.
func (s *Store) ServiceToken() string {
	return Sign(s.secret, Principal{
		Sub: "launcher", Role: RoleAdmin, Instances: []string{Everything},
		Iat: time.Now().Unix(), Exp: time.Now().Add(time.Hour).Unix(),
	})
}

// Verify checks a token against the household secret.
func (s *Store) Verify(token string) (Principal, error) { return Verify(s.secret, token) }

// Sign encodes and signs a principal: v1.<payload>.<hmac>.
func Sign(secret []byte, p Principal) string {
	payload, _ := json.Marshal(p)
	body := tokenPrefix + "." + base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(body))
	return body + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// Verify is the pure counterpart of Sign; the Python core implements the
// same check in auth.py.
func Verify(secret []byte, token string) (Principal, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 || parts[0] != tokenPrefix {
		return Principal{}, errors.New("malformed token")
	}
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(parts[0] + "." + parts[1]))
	want := mac.Sum(nil)
	got, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil || !hmac.Equal(got, want) {
		return Principal{}, errors.New("bad signature")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return Principal{}, errors.New("malformed token")
	}
	var p Principal
	if err := json.Unmarshal(raw, &p); err != nil {
		return Principal{}, errors.New("malformed token")
	}
	if p.Exp < time.Now().Unix() {
		return Principal{}, errors.New("token expired")
	}
	return p, nil
}
