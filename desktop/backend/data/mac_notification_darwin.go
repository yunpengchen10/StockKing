//go:build darwin

package data

import "os/exec"

// Notification text is passed as data, never interpolated into AppleScript.
func macNotificationCommand(title, content string) *exec.Cmd {
	const script = `on run argv
 display notification (item 2 of argv) with title (item 1 of argv)
end run`
	return exec.Command("/usr/bin/osascript", "-e", script, "--", title, content)
}
