// Package supervise runs one core process per household member and keeps
// it alive: start, health, restart with exponential backoff, stop cleanly.
// Deterministic and boring on purpose (ADR-0002, ADR-0012).
package supervise

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"sync"
	"time"
)

// Spec describes how to start one instance.
type Spec struct {
	ID      string
	Command []string          // e.g. ["uv", "run", "python", "-m", "aitmation_core"]
	Dir     string            // working directory (the core checkout or release)
	Env     map[string]string // AITMATION_* per instance; inherits the parent environment
	Port    int
}

type Status struct {
	ID        string    `json:"id"`
	Port      int       `json:"port"`
	Running   bool      `json:"running"`
	Healthy   bool      `json:"healthy"`
	Restarts  int       `json:"restarts"`
	LastError string    `json:"last_error,omitempty"`
	Since     time.Time `json:"since"`
}

type instance struct {
	spec   Spec
	cancel context.CancelFunc
	mu     sync.Mutex
	status Status
}

// Supervisor owns the set of running instances.
type Supervisor struct {
	log       *slog.Logger
	mu        sync.Mutex
	instances map[string]*instance
	ctx       context.Context
	wg        sync.WaitGroup
}

func New(ctx context.Context, log *slog.Logger) *Supervisor {
	return &Supervisor{log: log, instances: map[string]*instance{}, ctx: ctx}
}

// Start launches an instance (idempotent per id) and supervises it.
func (s *Supervisor) Start(spec Spec) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.instances[spec.ID]; ok {
		return
	}
	ctx, cancel := context.WithCancel(s.ctx)
	inst := &instance{spec: spec, cancel: cancel, status: Status{ID: spec.ID, Port: spec.Port}}
	s.instances[spec.ID] = inst
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		s.loop(ctx, inst)
	}()
}

// Stop cancels one instance and waits for its process to end.
func (s *Supervisor) Stop(id string) {
	s.mu.Lock()
	inst, ok := s.instances[id]
	if ok {
		delete(s.instances, id)
	}
	s.mu.Unlock()
	if ok {
		inst.cancel()
	}
}

// Wait blocks until every instance loop has ended (after context cancel).
func (s *Supervisor) Wait() { s.wg.Wait() }

// Statuses returns a snapshot for the status page and the API.
func (s *Supervisor) Statuses() []Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Status, 0, len(s.instances))
	for _, inst := range s.instances {
		inst.mu.Lock()
		out = append(out, inst.status)
		inst.mu.Unlock()
	}
	return out
}

func (s *Supervisor) loop(ctx context.Context, inst *instance) {
	backoff := time.Second
	for {
		if ctx.Err() != nil {
			return
		}
		started := time.Now()
		err := s.runOnce(ctx, inst)
		inst.mu.Lock()
		inst.status.Running = false
		inst.status.Healthy = false
		if err != nil {
			inst.status.LastError = err.Error()
		}
		inst.mu.Unlock()
		if ctx.Err() != nil {
			return
		}
		// A process that lived for a while gets a fresh backoff; a crash
		// loop waits longer each time, capped, so a broken install does
		// not spin.
		if time.Since(started) > 2*time.Minute {
			backoff = time.Second
		}
		s.log.Warn("instance ended, restarting", "id", inst.spec.ID, "error", err, "backoff", backoff)
		inst.mu.Lock()
		inst.status.Restarts++
		inst.mu.Unlock()
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff < time.Minute {
			backoff *= 2
		}
	}
}

func (s *Supervisor) runOnce(ctx context.Context, inst *instance) error {
	spec := inst.spec
	if len(spec.Command) == 0 {
		return fmt.Errorf("instance %s has no command", spec.ID)
	}
	cmd := exec.CommandContext(ctx, spec.Command[0], spec.Command[1:]...)
	cmd.Dir = spec.Dir
	cmd.Env = os.Environ()
	for k, v := range spec.Env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	// A cancelled context sends SIGINT first so the core can finish its
	// current tick (core README hard requirement 6); kill only as a last resort.
	// Windows cannot deliver SIGINT to a child, so there we kill at once
	// instead of waiting out WaitDelay with the port still taken.
	cmd.Cancel = func() error {
		if runtime.GOOS == "windows" {
			return cmd.Process.Kill()
		}
		return cmd.Process.Signal(os.Interrupt)
	}
	cmd.WaitDelay = 15 * time.Second
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start: %w", err)
	}
	inst.mu.Lock()
	inst.status.Running = true
	inst.status.Since = time.Now()
	inst.status.LastError = ""
	inst.mu.Unlock()
	s.log.Info("instance started", "id", spec.ID, "port", spec.Port, "pid", cmd.Process.Pid)

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case err := <-done:
			return err
		case <-ticker.C:
			healthy := probe(spec.Port)
			inst.mu.Lock()
			inst.status.Healthy = healthy
			inst.mu.Unlock()
		}
	}
}

// probe asks the core's health endpoint; it is the same check Coolify and
// the control plane use (docs/DEPLOYMENT.md consequence 5).
func probe(port int) bool {
	client := http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/healthz", port))
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// WaitHealthy polls until the instance answers or the deadline passes.
func WaitHealthy(ctx context.Context, port int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if probe(port) {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(500 * time.Millisecond):
		}
	}
	return false
}
