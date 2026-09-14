//go:build windows

package main

import (
	"os/exec"
	"syscall"
)

const createNoWindow = 0x08000000

func hideSidecarWindow(cmd *exec.Cmd) {
	if cmd == nil {
		return
	}
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	// HideWindow alone can still flash a console before Windows processes the
	// show-state hint. CREATE_NO_WINDOW prevents a console from being allocated
	// at all, including when development/fallback discovery starts python.exe.
	cmd.SysProcAttr.HideWindow = true
	cmd.SysProcAttr.CreationFlags |= createNoWindow
}
