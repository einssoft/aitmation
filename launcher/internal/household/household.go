// Package household describes one installation (ADR-0024): an admin, a
// mailroom and one or more mandants, each a core instance of its own
// (ADR-0001). The file household.toml in the data directory is the single
// source of truth; the launcher derives everything else from it -
// deterministically, no model involved (ADR-0012).
package household

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/BurntSushi/toml"
)

const (
	TypePrivate = "private"
	TypeCompany = "company"

	DefaultLauncherPort = 8400
	DefaultMailroomPort = 8420
	firstMandantPort    = 8421
)

type Launcher struct {
	Port int `toml:"port"`
}

type Mailroom struct {
	Port  int    `toml:"port"`
	Title string `toml:"title,omitempty"`
}

type Mandant struct {
	ID    string `toml:"id"`
	Title string `toml:"title"`
	Type  string `toml:"type"`
	Port  int    `toml:"port"`
}

type Household struct {
	Admin    string    `toml:"admin"`
	Launcher Launcher  `toml:"launcher"`
	Mailroom Mailroom  `toml:"mailroom"`
	Mandants []Mandant `toml:"mandants"`
}

var idPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)

// New returns an empty household with the default ports.
func New(admin string) *Household {
	return &Household{
		Admin:    admin,
		Launcher: Launcher{Port: DefaultLauncherPort},
		Mailroom: Mailroom{Port: DefaultMailroomPort, Title: "Eingang"},
	}
}

// Load reads household.toml; a missing file is reported as os.ErrNotExist.
func Load(path string) (*Household, error) {
	var h Household
	if _, err := toml.DecodeFile(path, &h); err != nil {
		return nil, err
	}
	if h.Launcher.Port == 0 {
		h.Launcher.Port = DefaultLauncherPort
	}
	if h.Mailroom.Port == 0 {
		h.Mailroom.Port = DefaultMailroomPort
	}
	if err := h.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &h, nil
}

// Save writes the file atomically (write to a sibling, then rename).
func (h *Household) Save(path string) error {
	if err := h.Validate(); err != nil {
		return err
	}
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o640)
	if err != nil {
		return err
	}
	enc := toml.NewEncoder(f)
	if err := enc.Encode(h); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Validate enforces the invariants the launcher relies on.
func (h *Household) Validate() error {
	seenID := map[string]bool{}
	seenPort := map[string]int{"launcher": h.Launcher.Port, "mailroom": h.Mailroom.Port}
	if h.Launcher.Port == h.Mailroom.Port {
		return errors.New("launcher and mailroom share a port")
	}
	for _, m := range h.Mandants {
		if !idPattern.MatchString(m.ID) {
			return fmt.Errorf("mandant id %q must match %s", m.ID, idPattern)
		}
		if m.ID == "mailroom" || m.ID == "launcher" {
			return fmt.Errorf("mandant id %q is reserved", m.ID)
		}
		if seenID[m.ID] {
			return fmt.Errorf("duplicate mandant id %q", m.ID)
		}
		seenID[m.ID] = true
		if m.Type != TypePrivate && m.Type != TypeCompany {
			return fmt.Errorf("mandant %q: type must be %s or %s", m.ID, TypePrivate, TypeCompany)
		}
		if strings.TrimSpace(m.Title) == "" {
			return fmt.Errorf("mandant %q needs a title", m.ID)
		}
		if m.Port <= 0 {
			return fmt.Errorf("mandant %q needs a port", m.ID)
		}
		for name, p := range seenPort {
			if p == m.Port {
				return fmt.Errorf("mandant %q: port %d already used by %s", m.ID, m.Port, name)
			}
		}
		seenPort[m.ID] = m.Port
	}
	return nil
}

// AddMandant appends a mandant with a derived id and the next free port.
func (h *Household) AddMandant(kind, title string) (*Mandant, error) {
	if kind != TypePrivate && kind != TypeCompany {
		return nil, fmt.Errorf("type must be %s or %s", TypePrivate, TypeCompany)
	}
	title = strings.TrimSpace(title)
	if title == "" {
		if kind == TypePrivate {
			title = "Privat"
		} else {
			return nil, errors.New("a company mandant needs a title")
		}
	}
	id := Slug(title)
	if id == "" || id == "mailroom" || id == "launcher" {
		id = kind
	}
	base := id
	for n := 2; h.byID(id) != nil; n++ {
		id = fmt.Sprintf("%s-%d", base, n)
	}
	m := Mandant{ID: id, Title: title, Type: kind, Port: h.nextPort()}
	h.Mandants = append(h.Mandants, m)
	if err := h.Validate(); err != nil {
		h.Mandants = h.Mandants[:len(h.Mandants)-1]
		return nil, err
	}
	return &h.Mandants[len(h.Mandants)-1], nil
}

func (h *Household) byID(id string) *Mandant {
	for i := range h.Mandants {
		if h.Mandants[i].ID == id {
			return &h.Mandants[i]
		}
	}
	return nil
}

func (h *Household) nextPort() int {
	used := map[int]bool{h.Launcher.Port: true, h.Mailroom.Port: true}
	for _, m := range h.Mandants {
		used[m.Port] = true
	}
	for p := firstMandantPort; ; p++ {
		if !used[p] {
			return p
		}
	}
}

// Slug turns a title into an id: lowercase ASCII, hyphens, umlauts folded.
func Slug(title string) string {
	r := strings.NewReplacer("ä", "ae", "ö", "oe", "ü", "ue", "Ä", "ae", "Ö", "oe", "Ü", "ue", "ß", "ss",
		"é", "e", "è", "e", "ê", "e", "à", "a", "â", "a", "ç", "c", "ô", "o", "î", "i", "û", "u", "ù", "u")
	s := strings.ToLower(r.Replace(strings.TrimSpace(title)))
	var b strings.Builder
	lastDash := true
	for _, c := range s {
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9':
			b.WriteRune(c)
			lastDash = false
		default:
			if !lastDash {
				b.WriteByte('-')
				lastDash = true
			}
		}
	}
	return strings.Trim(b.String(), "-")
}

// Layout: where each member keeps its data below the DataDir.
func (h *Household) MailroomDir(dataDir string) string { return filepath.Join(dataDir, "mailroom") }
func (h *Household) MandantDir(dataDir string, m Mandant) string {
	return filepath.Join(dataDir, "mandants", m.ID)
}

// AddresseesTOML renders the mailroom's addressee table: one folder channel
// per mandant, pointing at that mandant's inbox (docs/VERMITTLER.md).
func (h *Household) AddresseesTOML(dataDir string) string {
	ms := append([]Mandant(nil), h.Mandants...)
	sort.Slice(ms, func(i, j int) bool { return ms[i].ID < ms[j].ID })
	var b strings.Builder
	b.WriteString("# Written by the launcher from household.toml - edit that file instead.\n")
	for _, m := range ms {
		inbox := filepath.ToSlash(filepath.Join(h.MandantDir(dataDir, m), "inbox"))
		fmt.Fprintf(&b, "\n[%s]\ntitle = %q\nchannel = \"folder\"\npath = %q\n", m.ID, m.Title, inbox)
	}
	return b.String()
}
