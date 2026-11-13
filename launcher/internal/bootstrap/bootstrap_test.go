package bootstrap

import (
	"os"
	"path/filepath"
	"testing"
)

func TestBundledPythonNextToCore(t *testing.T) {
	bundle := t.TempDir()
	core := filepath.Join(bundle, "core")
	if err := os.MkdirAll(filepath.Join(core, "src"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := BundledPython(core); got != "" {
		t.Fatalf("no python dir yet, got %q", got)
	}
	py := filepath.Join(bundle, "python", "bin", "python3")
	if err := os.MkdirAll(filepath.Dir(py), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(py, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := BundledPython(core); got != py {
		t.Fatalf("want %q, got %q", py, got)
	}
	// a trailing separator on the core dir must not change the lookup
	if got := BundledPython(core + string(filepath.Separator)); got != py {
		t.Fatalf("with trailing separator: want %q, got %q", py, got)
	}
	env := CoreEnv(core)
	if env["PYTHONPATH"] != filepath.Join(core, "src") || env["PYTHONNOUSERSITE"] != "1" {
		t.Fatalf("unexpected env %v", env)
	}
	if _, ok := env["PYTHONHOME"]; !ok {
		t.Fatal("PYTHONHOME must be cleared explicitly")
	}
}
