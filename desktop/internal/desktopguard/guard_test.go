package desktopguard

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestRestartRequiresReadyUnexpectedExit(t *testing.T) {
	for _, test := range []struct {
		name                           string
		code                           uint32
		ready, closing, shutdown, want bool
	}{
		{"ordinary close", 0, true, false, false, false},
		{"second shortcut launch", 0, false, false, false, false},
		{"startup failure", 1, false, false, false, false},
		{"WebView crash", RecoveryExitCode, true, false, false, true},
		{"native crash", 0xc0000005, true, false, false, true},
		{"failure during deliberate cleanup", 1, true, true, false, false},
		{"session shutdown", RecoveryExitCode, true, false, true, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := shouldRestart(test.code, test.ready, test.closing, test.shutdown); got != test.want {
				t.Fatalf("restart=%v want=%v", got, test.want)
			}
		})
	}
}

func TestRecoveryBurstIsBoundedAndAgesOut(t *testing.T) {
	var budget restartBudget
	now := time.Date(2026, 10, 13, 10, 0, 0, 0, time.UTC)
	for attempt, want := range []time.Duration{time.Second, 2 * time.Second, 4 * time.Second} {
		if delay, allowed := budget.next(now.Add(time.Duration(attempt) * time.Second)); !allowed || delay != want {
			t.Fatalf("attempt %d: delay=%v allowed=%v", attempt, delay, allowed)
		}
	}
	if _, allowed := budget.next(now.Add(time.Minute)); allowed {
		t.Fatal("fourth recovery in two minutes allowed")
	}
	if delay, allowed := budget.next(now.Add(3 * time.Minute)); !allowed || delay != time.Second {
		t.Fatalf("expired failures still throttle recovery: %v %v", delay, allowed)
	}
}

func TestChildArgsPreserveInvocationExactly(t *testing.T) {
	original := []string{"--custom=space path", "中文", "quoted\"value"}
	got := childArguments(original)
	want := append(append([]string(nil), original...), ChildArgument)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("args=%q want=%q", got, want)
	}
	if !reflect.DeepEqual(childArguments(got), want) {
		t.Fatal("duplicate child marker appended")
	}
	if hasChildArgument([]string{ChildArgument + "-fake"}) {
		t.Fatal("prefix accepted as actual child marker")
	}
	got[0] = "changed"
	if original[0] != "--custom=space path" {
		t.Fatal("caller args mutated")
	}
}

func TestChildEnvironmentResetsStaleRecoveryState(t *testing.T) {
	base := []string{"PATH=unchanged", strings.ToLower(sessionEnv) + "=stale", minimizedEnv + "=1"}
	got := childEnvironment(base, "fresh", false)
	want := []string{"PATH=unchanged", sessionEnv + "=fresh", minimizedEnv + "=0"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("environment=%q", got)
	}
	if base[1] != strings.ToLower(sessionEnv)+"=stale" {
		t.Fatal("base environment mutated")
	}
}

func TestMarkersAreScopedToSupervisedAttempt(t *testing.T) {
	oldArgs := os.Args
	t.Cleanup(func() { os.Args = oldArgs })
	dir := t.TempDir()
	t.Setenv(sessionEnv, dir)
	t.Setenv(minimizedEnv, "1")
	os.Args = []string{"desktop"}
	MarkReady()
	if IsSupervised() || IsRecoveryMinimized() || exists(filepath.Join(dir, "ready")) {
		t.Fatal("unmanaged process accepted supervisor environment")
	}
	os.Args = []string{"desktop", ChildArgument}
	MarkReady()
	MarkReady()
	MarkClosing()
	if !IsSupervised() || !IsRecoveryMinimized() || !exists(filepath.Join(dir, "ready")) || !exists(filepath.Join(dir, "closing")) {
		t.Fatal("managed process did not record readiness/close")
	}
	t.Setenv(sessionEnv, "")
	if IsSupervised() {
		t.Fatal("child argument without session accepted")
	}
}

func TestCrashLogIsBoundedAndKeepsNewestOutput(t *testing.T) {
	dir := t.TempDir()
	w := rotatingWriter{path: filepath.Join(dir, "crash.log"), limit: 64}
	for index := 0; index < 12; index++ {
		if _, err := w.Write([]byte(strings.Repeat(string(rune('a'+index)), 40))); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 3 {
		t.Fatalf("expected current plus two backups: %d", len(entries))
	}
	for _, entry := range entries {
		info, _ := entry.Info()
		if info.Size() > 64 {
			t.Fatalf("unbounded %s: %d", entry.Name(), info.Size())
		}
	}
	oversized := strings.Repeat("x", 100) + "tail"
	if n, err := w.Write([]byte(oversized)); err != nil || n != len(oversized) {
		t.Fatalf("oversize write: %d %v", n, err)
	}
	latest, err := os.ReadFile(w.path)
	if err != nil || len(latest) != 64 || !strings.HasSuffix(string(latest), "tail") {
		t.Fatalf("lost latest crash output: %q %v", latest, err)
	}
}
