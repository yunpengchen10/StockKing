//go:build !windows

package desktopguard

func Run(logDir string) (handled bool, exitCode int) { return false, 0 }
