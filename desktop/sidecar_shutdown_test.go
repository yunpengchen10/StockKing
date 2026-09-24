package main

import (
	"context"
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestSidecarStopReapsProcess(t *testing.T) {
	if os.Getenv("STOCK_KING_STOP_PROBE") == "1" {
		time.Sleep(time.Minute)
		os.Exit(0)
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cmd := exec.CommandContext(ctx, executable, "-test.run=^TestSidecarStopReapsProcess$")
	cmd.Env = append(os.Environ(), "STOCK_KING_STOP_PROBE=1")
	hideSidecarWindow(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	m := NewSidecarManager(AppPaths{})
	m.cmd, m.cancel, m.done = cmd, cancel, make(chan struct{})
	done := m.done
	go func() { _ = cmd.Wait(); close(done) }()
	m.Stop()
	if cmd.ProcessState == nil {
		t.Fatal("Stop returned before child was reaped")
	}
	// A late supervisor result cannot turn a stopped engine into an error.
	m.setStatus(EngineStatus{State: "restarting", Message: "signal: killed"})
	if status := m.Status(); status.State != "stopped" || status.Ready {
		t.Fatalf("unexpected final state: %+v", status)
	}
	m.Stop()
}
