//go:build windows

package desktopguard

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

const probeEnv = "STOCK_KING_GUARD_TEST_PROBE"

func TestMain(m *testing.M) {
	if mode := os.Getenv(probeEnv); mode != "" {
		switch mode {
		case "ready_crash":
			MarkReady()
			fmt.Fprintln(os.Stderr, "captured native crash evidence")
			os.Exit(RecoveryExitCode)
		case "close_crash":
			MarkReady()
			MarkClosing()
			os.Exit(RecoveryExitCode)
		case "bulk_crash":
			// Far larger than a Windows anonymous pipe buffer. If its reader
			// stops on a log error, this write blocks and ready is never reached.
			if _, err := os.Stdout.Write(bytes.Repeat([]byte("x"), 2*1024*1024)); err != nil {
				os.Exit(5)
			}
			MarkReady()
			os.Exit(RecoveryExitCode)
		case "wait":
			time.Sleep(time.Minute)
			os.Exit(0)
		case "descendant":
			exe, _ := os.Executable()
			cmd := exec.Command(exe)
			cmd.Env = append(os.Environ(), probeEnv+"=wait")
			cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW}
			if err := cmd.Start(); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(3)
			}
			if err := os.WriteFile(os.Getenv("STOCK_KING_GUARD_TEST_PID"), []byte(strconv.Itoa(cmd.Process.Pid)), 0600); err != nil {
				os.Exit(4)
			}
			MarkReady()
			os.Exit(RecoveryExitCode)
		}
		os.Exit(2)
	}
	os.Exit(m.Run())
}

type failingEvidenceWriter struct{ attempted atomic.Int64 }

func (w *failingEvidenceWriter) Write(p []byte) (int, error) {
	w.attempted.Add(int64(len(p)))
	return 0, errors.New("simulated full/locked evidence file")
}

func TestEvidenceWriteFailureStillDrainsChildPipe(t *testing.T) {
	output := &failingEvidenceWriter{}
	child, session := testChild(t, "bulk_crash", output)
	type result struct {
		code uint32
		err  error
	}
	finished := make(chan result, 1)
	go func() { code, _, err := child.wait(false); finished <- result{code, err} }()
	select {
	case exit := <-finished:
		child.close()
		if exit.err != nil || exit.code != RecoveryExitCode {
			t.Fatalf("child exit=%d error=%v", exit.code, exit.err)
		}
	case <-time.After(5 * time.Second):
		child.close()
		t.Fatal("log failure stopped pipe draining and blocked the desktop")
	}
	if !exists(filepath.Join(session, "ready")) {
		t.Fatal("child did not finish writing output")
	}
	if output.attempted.Load() < 2*1024*1024 {
		t.Fatalf("stopped attempting evidence output after %d bytes", output.attempted.Load())
	}
}

func testChild(t *testing.T, mode string, output io.Writer, extra ...string) (*childProcess, string) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	session := t.TempDir()
	env := childEnvironment(append(os.Environ(), append([]string{probeEnv + "=" + mode}, extra...)...), session, false)
	child, err := startChild(exe, []string{ChildArgument}, filepath.Dir(exe), env, output)
	if err != nil {
		t.Fatal(err)
	}
	return child, session
}

func TestRealChildCrashCapturesEvidenceAndReadyMarker(t *testing.T) {
	var output bytes.Buffer
	child, session := testChild(t, "ready_crash", &output)
	code, _, err := child.wait(false)
	child.close()
	if err != nil || code != RecoveryExitCode {
		t.Fatalf("exit=%d error=%v output=%s", code, err, output.String())
	}
	if !exists(filepath.Join(session, "ready")) || !shouldRestart(code, true, false, false) {
		t.Fatal("ready crash was not recoverable")
	}
	if !strings.Contains(output.String(), "captured native crash evidence") {
		t.Fatalf("lost child stderr: %q", output.String())
	}
}

func TestRealDeliberateCloseOverridesCrashExit(t *testing.T) {
	child, session := testChild(t, "close_crash", io.Discard)
	code, _, err := child.wait(false)
	child.close()
	if err != nil || code != RecoveryExitCode {
		t.Fatalf("exit=%d error=%v", code, err)
	}
	if shouldRestart(code, exists(filepath.Join(session, "ready")), exists(filepath.Join(session, "closing")), false) {
		t.Fatal("deliberately closed process would reopen")
	}
}

func TestOwnedJobCleansDescendantWithoutTouchingUnrelatedProcess(t *testing.T) {
	exe, _ := os.Executable()
	unrelated := exec.Command(exe)
	unrelated.Env = append(os.Environ(), probeEnv+"=wait")
	unrelated.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW}
	if err := unrelated.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = unrelated.Process.Kill(); _ = unrelated.Wait() })
	unrelatedHandle, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(unrelated.Process.Pid))
	if err != nil {
		t.Fatal(err)
	}
	defer windows.CloseHandle(unrelatedHandle)
	pidFile := filepath.Join(t.TempDir(), "child-pid")
	child, _ := testChild(t, "descendant", io.Discard, "STOCK_KING_GUARD_TEST_PID="+pidFile)
	code, _, err := child.wait(false)
	if err != nil || code != RecoveryExitCode {
		child.close()
		t.Fatalf("helper exit=%d %v", code, err)
	}
	pidData, err := os.ReadFile(pidFile)
	if err != nil {
		child.close()
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(string(pidData))
	if err != nil {
		child.close()
		t.Fatal(err)
	}
	descendant, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		child.close()
		t.Fatal(err)
	}
	defer windows.CloseHandle(descendant)
	if result, _ := windows.WaitForSingleObject(descendant, 0); result != uint32(windows.WAIT_TIMEOUT) {
		child.close()
		t.Fatal("descendant was not alive before job cleanup")
	}
	child.close()
	if result, err := windows.WaitForSingleObject(descendant, 3000); err != nil || result != windows.WAIT_OBJECT_0 {
		t.Fatalf("owned descendant survived cleanup: %d %v", result, err)
	}
	if result, err := windows.WaitForSingleObject(unrelatedHandle, 0); err != nil || result != uint32(windows.WAIT_TIMEOUT) {
		t.Fatalf("unrelated process affected: %d %v", result, err)
	}
}

func TestShutdownWindowPreventsRecoveryAndHandlesCancellation(t *testing.T) {
	watcher := watchSessionShutdown()
	defer watcher.close()
	if watcher.window == 0 {
		t.Fatal("shutdown notification window was not created")
	}
	sendMessage := user32.NewProc("SendMessageW")
	_, _, _ = sendMessage.Call(watcher.window, 0x0011, 0, 0)
	if !watcher.active() {
		t.Fatal("session end did not suppress recovery")
	}
	_, _, _ = sendMessage.Call(watcher.window, 0x0016, 0, 0)
	if watcher.active() {
		t.Fatal("cancelled session end still suppresses recovery")
	}
}

func TestWindowPollingReusesCallback(t *testing.T) {
	for index := 0; index < 2500; index++ {
		if _, found := minimizedWindow(0); found {
			t.Fatal("matched unrelated window")
		}
	}
}
