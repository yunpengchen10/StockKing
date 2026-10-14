package main

import (
	"go-stock/internal/desktopguard"
	"io"
	"log"
	"path/filepath"
	"strings"

	"github.com/wailsapp/wails/v2/pkg/logger"
	"gopkg.in/natefinch/lumberjack.v2"
)

// Wails v2.11 does not expose a ProcessFailed hook. Its browser-failure
// branch logs this message immediately before a blocking dialog and os.Exit.
// Handle only the two fatal process kinds at that boundary; GPU/utility
// failures recover inside WebView2 and a busy renderer is not a crash.
type desktopLogger struct {
	output     *log.Logger
	supervised bool
	exit       func(int)
}

func newDesktopLogger(logDir string, supervised bool, exit func(int)) logger.Logger {
	return desktopLoggerTo(&lumberjack.Logger{
		Filename: filepath.Join(logDir, "wails.log"),
		MaxSize:  5, MaxBackups: 3,
	}, supervised, exit)
}

func desktopLoggerTo(output io.Writer, supervised bool, exit func(int)) *desktopLogger {
	return &desktopLogger{output: log.New(output, "", log.LstdFlags|log.Lmicroseconds), supervised: supervised, exit: exit}
}

func (l *desktopLogger) Print(message string)   { l.output.Print(message) }
func (l *desktopLogger) Trace(message string)   { l.output.Print("TRACE | " + message) }
func (l *desktopLogger) Debug(message string)   { l.output.Print("DEBUG | " + message) }
func (l *desktopLogger) Info(message string)    { l.output.Print("INFO  | " + message) }
func (l *desktopLogger) Warning(message string) { l.output.Print("WARN  | " + message) }
func (l *desktopLogger) Error(message string) {
	l.output.Print("ERROR | " + message)
	if l.supervised && fatalWebViewFailure(message) {
		l.output.Print("RECOVER | WebView2 exited; desktop supervisor will restore the window")
		l.exit(desktopguard.RecoveryExitCode)
	}
}
func (l *desktopLogger) Fatal(message string) {
	l.output.Print("FATAL | " + message)
	l.exit(1)
}

func fatalWebViewFailure(message string) bool {
	switch strings.TrimSpace(message) {
	case "WebVie2wProcess failed with kind 0", "WebVie2wProcess failed with kind 1":
		return true
	default:
		return false
	}
}
