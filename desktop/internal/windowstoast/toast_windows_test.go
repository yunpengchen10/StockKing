package toast

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNotificationPowerShellHasNoConsole(t *testing.T) {
	// Exercise the actual notification launcher without sending a notification.
	file := filepath.Join(t.TempDir(), "console probe.ps1")
	script := `Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class ConsoleProbe { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); }'
if ([ConsoleProbe]::GetConsoleWindow() -ne [IntPtr]::Zero) { exit 12 }
Write-Output 'console-free'
`
	if err := os.WriteFile(file, []byte(script), 0600); err != nil {
		t.Fatal(err)
	}
	cmd := notificationCommand(file)
	if cmd.SysProcAttr.CreationFlags&0x08000000 == 0 {
		t.Fatal("notification must prevent console creation, not just hide it")
	}
	output, err := cmd.CombinedOutput()
	if err != nil || strings.TrimSpace(string(output)) != "console-free" {
		t.Fatalf("PowerShell console probe: %v: %s", err, output)
	}
}
