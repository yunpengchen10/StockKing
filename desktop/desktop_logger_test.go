package main

import (
	"bytes"
	"fmt"
	"go-stock/internal/desktopguard"
	"strings"
	"testing"
)

func TestDesktopLoggerRecoversOnlyFatalWebViewFailures(t *testing.T) {
	for _, supervised := range []bool{false, true} {
		for kind := 0; kind <= 9; kind++ {
			t.Run(fmt.Sprintf("supervised=%t/kind=%d", supervised, kind), func(t *testing.T) {
				var output bytes.Buffer
				code := 0
				log := desktopLoggerTo(&output, supervised, func(value int) { code = value })
				message := fmt.Sprintf("WebVie2wProcess failed with kind %d", kind)
				log.Error(message)
				want := 0
				if supervised && (kind == 0 || kind == 1) {
					want = desktopguard.RecoveryExitCode
				}
				if code != want {
					t.Fatalf("exit=%d, want %d", code, want)
				}
				if !strings.Contains(output.String(), message) {
					t.Fatal("lost failure evidence")
				}
			})
		}
	}
}

func TestDesktopLoggerKeepsOrdinaryErrorsAndTimestamps(t *testing.T) {
	var output bytes.Buffer
	log := desktopLoggerTo(&output, true, func(int) { t.Fatal("ordinary error terminated desktop") })
	log.Error("request failed: WebVie2wProcess failed with kind 0")
	log.Info("ready")
	if !strings.Contains(output.String(), "INFO  | ready") || len(output.String()) < 20 || output.String()[4] != '/' {
		t.Fatalf("missing timestamp or level: %q", output.String())
	}
}
