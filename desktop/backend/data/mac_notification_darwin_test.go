//go:build darwin

package data

import (
	"strings"
	"testing"
)

func TestMacNotificationCommand(t *testing.T) {
	title := `A "quoted" title`
	content := `"; do shell script "unexpected command"`
	cmd := macNotificationCommand(title, content)
	if len(cmd.Args) != 6 || cmd.Args[3] != "--" || cmd.Args[4] != title || cmd.Args[5] != content {
		t.Fatalf("notification text was not preserved as arguments: %#v", cmd.Args)
	}
	if strings.Contains(cmd.Args[2], title) || strings.Contains(cmd.Args[2], content) {
		t.Fatal("untrusted notification text was interpolated into AppleScript")
	}
}
