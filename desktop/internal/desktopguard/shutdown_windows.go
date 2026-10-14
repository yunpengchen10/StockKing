//go:build windows

package desktopguard

import (
	"runtime"
	"sync/atomic"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	registerClass    = user32.NewProc("RegisterClassExW")
	createWindow     = user32.NewProc("CreateWindowExW")
	defWindowProc    = user32.NewProc("DefWindowProcW")
	getMessage       = user32.NewProc("GetMessageW")
	translateMessage = user32.NewProc("TranslateMessage")
	dispatchMessage  = user32.NewProc("DispatchMessageW")
	postMessage      = user32.NewProc("PostMessageW")
	postQuitMessage  = user32.NewProc("PostQuitMessage")
	sessionEnding    atomic.Bool
)

type windowClass struct {
	size        uint32
	style       uint32
	procedure   uintptr
	classExtra  int32
	windowExtra int32
	instance    windows.Handle
	icon        windows.Handle
	cursor      windows.Handle
	background  windows.Handle
	menu        *uint16
	name        *uint16
	smallIcon   windows.Handle
}

type nativeMessage struct {
	window  windows.Handle
	message uint32
	wParam  uintptr
	lParam  uintptr
	time    uint32
	x, y    int32
	private uint32
}

var shutdownWindowProcedure = windows.NewCallback(func(hwnd, message, wParam, lParam uintptr) uintptr {
	switch message {
	case 0x0011: // WM_QUERYENDSESSION: logoff/shutdown has begun.
		sessionEnding.Store(true)
		return 1
	case 0x0016: // WM_ENDSESSION: false means that shutdown was cancelled.
		sessionEnding.Store(wParam != 0)
	case 0x0002: // WM_DESTROY
		_, _, _ = postQuitMessage.Call(0)
		return 0
	}
	result, _, _ := defWindowProc.Call(hwnd, message, wParam, lParam)
	return result
})

type shutdownWatcher struct{ window uintptr }

// An invisible top-level window receives Windows' session-end broadcasts. A
// message-only window would not receive them. It has no taskbar button or UI.
func watchSessionShutdown() shutdownWatcher {
	ready := make(chan uintptr, 1)
	go func() {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		name, _ := windows.UTF16PtrFromString("StockKingDesktopSessionGuard")
		class := windowClass{procedure: shutdownWindowProcedure, name: name}
		class.size = uint32(unsafe.Sizeof(class))
		registered, _, err := registerClass.Call(uintptr(unsafe.Pointer(&class)))
		if registered == 0 && err != windows.ERROR_CLASS_ALREADY_EXISTS {
			ready <- 0
			return
		}
		hwnd, _, _ := createWindow.Call(0x00000080, uintptr(unsafe.Pointer(name)), uintptr(unsafe.Pointer(name)), 0, 0, 0, 0, 0, 0, 0, 0, 0)
		ready <- hwnd
		if hwnd == 0 {
			return
		}
		var message nativeMessage
		for {
			result, _, _ := getMessage.Call(uintptr(unsafe.Pointer(&message)), 0, 0, 0)
			if result == 0 || int32(result) == -1 {
				return
			}
			_, _, _ = translateMessage.Call(uintptr(unsafe.Pointer(&message)))
			_, _, _ = dispatchMessage.Call(uintptr(unsafe.Pointer(&message)))
		}
	}()
	return shutdownWatcher{window: <-ready}
}

func (w shutdownWatcher) active() bool {
	if sessionEnding.Load() {
		return true
	}
	shuttingDown, _, _ := getSystemMetrics.Call(0x2000) // SM_SHUTTINGDOWN
	return shuttingDown != 0
}

func (w shutdownWatcher) close() {
	if w.window != 0 {
		_, _, _ = postMessage.Call(w.window, 0x0010, 0, 0)
	}
}
