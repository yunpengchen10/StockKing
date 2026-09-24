package main

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
)

func TestShutdownWithoutLiveWindowIsIdempotent(t *testing.T) {
	var summaryStops, agentStops atomic.Int32
	app := &App{
		// A non-nil context with no Wails frontend models a destroyed window.
		ctx:           context.Background(),
		summaryCancel: func() { summaryStops.Add(1) },
		agentCancel:   func() { agentStops.Add(1) },
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); app.shutdown(context.Background()) }()
	}
	wg.Wait()
	if summaryStops.Load() != 1 || agentStops.Load() != 1 {
		t.Fatalf("cleanup repeated: summary=%d agent=%d", summaryStops.Load(), agentStops.Load())
	}
}
