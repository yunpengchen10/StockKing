//go:build darwin

package main

/*
#cgo LDFLAGS: -framework CoreGraphics
#include <CoreGraphics/CoreGraphics.h>
*/
import "C"

import (
	"os/exec"
)

func hideSidecarWindow(cmd *exec.Cmd) {}

func getScreenResolution() (int, int, int, int, error) {
	bounds := C.CGDisplayBounds(C.CGMainDisplayID())
	w, h := int(bounds.size.width), int(bounds.size.height)
	if w <= 0 || h <= 0 {
		w, h = 1412, 834
	}
	return w, h, w * 2 / 5, h * 2 / 5, nil
}
