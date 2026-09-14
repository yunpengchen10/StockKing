//go:build windows

package data

import (
	"os"
	"os/exec"
	"syscall"
	"testing"
)

func TestBackgroundBrowserDoesNotAllocateConsole(t *testing.T) {
	if os.Getenv("STOCK_KING_CONSOLE_PROBE") == "1" {
		window, _, _ := syscall.NewLazyDLL("kernel32.dll").NewProc("GetConsoleWindow").Call()
		if window != 0 {
			os.Exit(2)
		}
		os.Exit(0)
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(executable, "-test.run=^TestBackgroundBrowserDoesNotAllocateConsole$")
	cmd.Env = append(os.Environ(), "STOCK_KING_CONSOLE_PROBE=1")
	hideBrowserProcessWindow(cmd)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("background helper unexpectedly owns a console: %v %s", err, output)
	}
}

func TestBackgroundBrowserKeepsFlagsAndCapturesOutput(t *testing.T) {
	cmd := exec.Command("cmd.exe", "/d", "/c", "echo stock-king-background")
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x00000200}
	hideBrowserProcessWindow(cmd)
	if !cmd.SysProcAttr.HideWindow || cmd.SysProcAttr.CreationFlags&0x08000200 != 0x08000200 {
		t.Fatal("browser console suppression must preserve existing process flags")
	}
	output, err := cmd.Output()
	if err != nil || string(output) != "stock-king-background\r\n" {
		t.Fatalf("hidden process output: %q, error: %v", output, err)
	}
}
