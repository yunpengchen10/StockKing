// Package desktopguard keeps a Windows desktop session available after an
// unexpected process failure. It never supervises scheduled/headless work.
package desktopguard

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	ChildArgument    = "--stock-king-desktop-child"
	RecoveryExitCode = 75
	sessionEnv       = "STOCK_KING_DESKTOP_SESSION"
	minimizedEnv     = "STOCK_KING_RECOVERY_MINIMIZED"
	maxLogBytes      = 2 * 1024 * 1024
)

func hasChildArgument(args []string) bool {
	for _, arg := range args {
		if arg == ChildArgument {
			return true
		}
	}
	return false
}

func childArguments(args []string) []string {
	result := append([]string(nil), args...)
	if !hasChildArgument(result) {
		result = append(result, ChildArgument)
	}
	return result
}

// IsSupervised is true only in a child started by this guard. A child flag alone
// (for example a manually invoked diagnostic process) does not enable recovery.
func IsSupervised() bool {
	return hasChildArgument(os.Args[1:]) && os.Getenv(sessionEnv) != ""
}

func IsRecoveryMinimized() bool { return IsSupervised() && os.Getenv(minimizedEnv) == "1" }

// MarkReady enables crash recovery only once the actual application is usable.
// Failed startup and a second shortcut launch must not create restart loops.
func MarkReady() { mark("ready") }

// MarkClosing is called before deliberate window shutdown, before cleanup that
// might itself fail. Such a process must stay closed regardless of its exit code.
func MarkClosing() { mark("closing") }

func mark(name string) {
	if !IsSupervised() {
		return
	}
	// Marker contents are irrelevant; existence is atomic and enough for the
	// owning supervisor. Every launch receives a fresh private directory.
	f, err := os.OpenFile(filepath.Join(os.Getenv(sessionEnv), name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err == nil {
		_ = f.Close()
	}
}

func exists(path string) bool { _, err := os.Stat(path); return err == nil }

func shouldRestart(exitCode uint32, ready, closing, shuttingDown bool) bool {
	return exitCode != 0 && ready && !closing && !shuttingDown
}

// restartBudget allows three recoveries in any two minute window. Repeated
// failures then remain stopped with an inspectable log instead of a crash loop.
type restartBudget struct{ attempts []time.Time }

func (b *restartBudget) next(now time.Time) (time.Duration, bool) {
	cutoff := now.Add(-2 * time.Minute)
	kept := b.attempts[:0]
	for _, previous := range b.attempts {
		if previous.After(cutoff) {
			kept = append(kept, previous)
		}
	}
	b.attempts = kept
	if len(b.attempts) >= 3 {
		return 0, false
	}
	delay := time.Second << len(b.attempts)
	b.attempts = append(b.attempts, now)
	return delay, true
}

func childEnvironment(base []string, session string, minimized bool) []string {
	result := make([]string, 0, len(base)+2)
	for _, item := range base {
		key, _, _ := strings.Cut(item, "=")
		if strings.EqualFold(key, sessionEnv) || strings.EqualFold(key, minimizedEnv) {
			continue
		}
		result = append(result, item)
	}
	state := "0"
	if minimized {
		state = "1"
	}
	return append(result, sessionEnv+"="+session, minimizedEnv+"="+state)
}

// rotatingWriter bounds both normal lifecycle evidence and child stderr (which
// captures native/Go crashes that bypass application logging). The process that
// owns this writer alone opens its files; children inherit only a pipe handle.
type rotatingWriter struct {
	mu    sync.Mutex
	path  string
	limit int64
}

// drainWriter keeps a child's stdout/stderr pipe flowing even when evidence
// cannot be written (disk full, antivirus locks, concurrent rotation, etc.).
// Logging failure must not freeze the desktop behind a full pipe. Each chunk
// gets one best-effort write, so later chunks recover if the filesystem does.
type drainWriter struct{ destination io.Writer }

func (w drainWriter) Write(p []byte) (int, error) {
	if w.destination != nil {
		_, _ = w.destination.Write(p)
	}
	return len(p), nil
}

func (w *rotatingWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	n := len(p)
	limit := w.limit
	if limit <= 0 {
		limit = maxLogBytes
	}
	if int64(len(p)) > limit {
		p = p[len(p)-int(limit):]
	}
	if info, err := os.Stat(w.path); err == nil && info.Size()+int64(len(p)) > limit {
		if err := os.Remove(w.path + ".2"); err != nil && !os.IsNotExist(err) {
			return 0, err
		}
		if err := os.Rename(w.path+".1", w.path+".2"); err != nil && !os.IsNotExist(err) {
			return 0, err
		}
		if err := os.Rename(w.path, w.path+".1"); err != nil {
			return 0, err
		}
	}
	f, err := os.OpenFile(w.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return 0, err
	}
	written, err := f.Write(p)
	closeErr := f.Close()
	if err != nil {
		return 0, err
	}
	if written != len(p) {
		return 0, io.ErrShortWrite
	}
	return n, closeErr
}

func lifecycle(w io.Writer, format string, args ...any) {
	_, _ = fmt.Fprintf(w, "%s %s\n", time.Now().Format(time.RFC3339Nano), fmt.Sprintf(format, args...))
}
