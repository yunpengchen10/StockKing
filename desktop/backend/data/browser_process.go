package data

import (
	"context"
	"github.com/chromedp/chromedp"
)

// All data crawlers launch browser helpers through this allocator. Headless
// hides browser UI; Windows process flags also prevent a console flashing.
func newBackgroundBrowserAllocator(parent context.Context, opts ...chromedp.ExecAllocatorOption) (context.Context, context.CancelFunc) {
	opts = append(opts, backgroundBrowserOptions()...)
	return chromedp.NewExecAllocator(parent, opts...)
}
