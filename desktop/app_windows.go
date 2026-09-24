//go:build windows
// +build windows

package main

import (
	"fmt"
	"go-stock/backend/logger"
	"syscall"
	"time"
	"unsafe"

	"github.com/wailsapp/wails/v2/pkg/options"
	"go-stock/internal/windowstoast"
)

const (
	monitorDefaultToPrimary = 1
	mdtEffectiveDpi         = 0
	logicalDpi              = 96
)

func OnSecondInstanceLaunch(secondInstanceData options.SecondInstanceData) {
	notification := toast.Notification{
		AppID:    "Stock King",
		Title:    "Stock King",
		Message:  "程序已经在运行了",
		Icon:     "",
		Duration: "short",
		Audio:    toast.Default,
	}
	err := notification.Push()
	if err != nil {
		logger.SugaredLogger.Error(err)
	}
	time.Sleep(time.Second * 3)
}

func getFrameless() bool {
	return true
}

// getScreenResolution 返回主屏逻辑尺寸（考虑 Windows DPI 缩放），用于自适应窗口大小。
// 返回：width, height, minWidth, minHeight。
func getScreenResolution() (int, int, int, int, error) {
	user32 := syscall.NewLazyDLL("user32.dll")
	monitorFromPoint := user32.NewProc("MonitorFromPoint")
	getMonitorInfo := user32.NewProc("GetMonitorInfoW")

	// 主屏原点
	pt := struct{ x, y int32 }{0, 0}
	hm, _, _ := monitorFromPoint.Call(uintptr(unsafe.Pointer(&pt)), monitorDefaultToPrimary)
	if hm == 0 {
		return getScreenResolutionFallback()
	}

	// MONITORINFO: cbSize, rcMonitor(RECT), rcWork(RECT), dwFlags
	// RECT: left, top, right, bottom (4 * int32)
	const miSize = 40
	mi := make([]byte, miSize)
	*(*uint32)(unsafe.Pointer(&mi[0])) = miSize
	ret, _, _ := getMonitorInfo.Call(hm, uintptr(unsafe.Pointer(&mi[0])))
	if ret == 0 {
		return getScreenResolutionFallback()
	}
	// rcMonitor: left, top, right, bottom (offset 4)
	left := *(*int32)(unsafe.Pointer(&mi[4]))
	top := *(*int32)(unsafe.Pointer(&mi[8]))
	right := *(*int32)(unsafe.Pointer(&mi[12]))
	bottom := *(*int32)(unsafe.Pointer(&mi[16]))
	physW := int(right - left)
	physH := int(bottom - top)
	if physW <= 0 || physH <= 0 {
		return getScreenResolutionFallback()
	}

	// 主屏 DPI（考虑缩放）
	shcore := syscall.NewLazyDLL("Shcore.dll")
	getDpiForMonitor := shcore.NewProc("GetDpiForMonitor")
	var dpiX, dpiY uintptr
	hr, _, _ := getDpiForMonitor.Call(hm, mdtEffectiveDpi, uintptr(unsafe.Pointer(&dpiX)), uintptr(unsafe.Pointer(&dpiY)))
	if hr != 0 || dpiX == 0 || dpiY == 0 {
		return getScreenResolutionFallback()
	}
	// 逻辑尺寸 = 物理尺寸 * 96 / DPI
	w := physW * logicalDpi / int(dpiX)
	h := physH * logicalDpi / int(dpiY)
	if w <= 0 || h <= 0 {
		return getScreenResolutionFallback()
	}
	minW := w * 2 / 5
	minH := h * 2 / 5
	return w, h, minW, minH, nil
}

// getScreenResolutionFallback 在 DPI 查询失败时使用 GetSystemMetrics 的回退逻辑（可能与缩放不一致）
func getScreenResolutionFallback() (int, int, int, int, error) {
	user32 := syscall.NewLazyDLL("user32.dll")
	getSystemMetrics := user32.NewProc("GetSystemMetrics")
	screenWidth, _, _ := getSystemMetrics.Call(0)  // SM_CXSCREEN
	screenHeight, _, _ := getSystemMetrics.Call(1) // SM_CYSCREEN
	if screenWidth == 0 || screenHeight == 0 {
		return 1000, 800, 900, 600, fmt.Errorf("getSystemMetrics failed")
	}
	w := int(screenWidth)
	h := int(screenHeight)
	minW := w * 2 / 5
	minH := h * 2 / 5
	return w, h, minW, minH, nil
}
