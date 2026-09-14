//go:build windows

package main

import (
	"os/exec"
	"syscall"
	"testing"
)

func TestHideSidecarWindowPreventsConsoleAllocation(t *testing.T) {
	cmd := exec.Command("cmd.exe", "/c", "exit", "0")
	hideSidecarWindow(cmd)

	if cmd.SysProcAttr == nil {
		t.Fatal("SysProcAttr was not configured")
	}
	if !cmd.SysProcAttr.HideWindow {
		t.Fatal("HideWindow must remain enabled as a fallback")
	}
	if cmd.SysProcAttr.CreationFlags&createNoWindow == 0 {
		t.Fatal("CREATE_NO_WINDOW must be enabled to prevent console flashes")
	}
}

func TestHideSidecarWindowPreservesExistingCreationFlags(t *testing.T) {
	const existingFlag = 0x00000200
	cmd := exec.Command("cmd.exe", "/c", "exit", "0")
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: existingFlag}

	hideSidecarWindow(cmd)

	if cmd.SysProcAttr.CreationFlags&existingFlag == 0 {
		t.Fatal("existing creation flags were overwritten")
	}
	if cmd.SysProcAttr.CreationFlags&createNoWindow == 0 {
		t.Fatal("CREATE_NO_WINDOW was not added")
	}
}

func TestHideSidecarWindowAcceptsNil(t *testing.T) {
	hideSidecarWindow(nil)
}
