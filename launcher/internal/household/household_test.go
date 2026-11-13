package household

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAddMandantAllocatesIdsAndPorts(t *testing.T) {
	h := New("alex")
	p, err := h.AddMandant(TypePrivate, "")
	if err != nil {
		t.Fatal(err)
	}
	if p.ID != "privat" || p.Port != 8421 || p.Title != "Privat" {
		t.Fatalf("unexpected private mandant %+v", *p)
	}
	c, err := h.AddMandant(TypeCompany, "Müller Malerei AG")
	if err != nil {
		t.Fatal(err)
	}
	if c.ID != "mueller-malerei-ag" || c.Port != 8422 {
		t.Fatalf("unexpected company mandant %+v", *c)
	}
	c2, _ := h.AddMandant(TypeCompany, "Müller Malerei AG")
	if c2.ID != "mueller-malerei-ag-2" {
		t.Fatalf("duplicate title should get a numbered id, got %q", c2.ID)
	}
	if _, err := h.AddMandant(TypeCompany, ""); err == nil {
		t.Fatal("company without title must fail")
	}
	if _, err := h.AddMandant("club", "x"); err == nil {
		t.Fatal("unknown type must fail")
	}
}

func TestSaveLoadRoundtrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "household.toml")
	h := New("alex")
	h.AddMandant(TypePrivate, "")
	h.AddMandant(TypeCompany, "Firma X")
	if err := h.Save(path); err != nil {
		t.Fatal(err)
	}
	loaded, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Admin != "alex" || len(loaded.Mandants) != 2 || loaded.Mandants[1].Port != 8422 {
		t.Fatalf("roundtrip lost data: %+v", *loaded)
	}
	if loaded.Launcher.Port != DefaultLauncherPort || loaded.Mailroom.Port != DefaultMailroomPort {
		t.Fatalf("default ports missing: %+v", *loaded)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatal("temp file left behind")
	}
}

func TestValidateRejectsCollisions(t *testing.T) {
	h := New("a")
	h.Mandants = []Mandant{{ID: "x", Title: "X", Type: TypeCompany, Port: DefaultMailroomPort}}
	if err := h.Validate(); err == nil || !strings.Contains(err.Error(), "port") {
		t.Fatalf("port collision not detected: %v", err)
	}
	h.Mandants = []Mandant{{ID: "mailroom", Title: "X", Type: TypeCompany, Port: 9000}}
	if err := h.Validate(); err == nil || !strings.Contains(err.Error(), "reserved") {
		t.Fatalf("reserved id not detected: %v", err)
	}
}

func TestAddresseesTOML(t *testing.T) {
	h := New("a")
	h.AddMandant(TypePrivate, "")
	h.AddMandant(TypeCompany, "Firma X")
	out := h.AddresseesTOML("/data")
	for _, want := range []string{"[privat]", "[firma-x]", `channel = "folder"`, "/data/mandants/firma-x/inbox"} {
		if !strings.Contains(out, want) {
			t.Fatalf("addressees.toml missing %q:\n%s", want, out)
		}
	}
}
