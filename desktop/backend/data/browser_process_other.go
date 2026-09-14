//go:build !windows

package data

import "github.com/chromedp/chromedp"

func backgroundBrowserOptions() []chromedp.ExecAllocatorOption { return nil }
