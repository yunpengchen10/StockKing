package main

import (
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestDesktopInstanceFollowsDataProfile(t *testing.T) {
	root := t.TempDir()
	profile := AppPaths{RoamingRoot: filepath.Join(root, "Stock King")}
	if desktopInstanceID(profile) == desktopInstanceID(AppPaths{RoamingRoot: filepath.Join(root, "other")}) {
		t.Fatal("independent data profiles share a desktop lock")
	}
	alias := AppPaths{RoamingRoot: profile.RoamingRoot + string(filepath.Separator) + "."}
	if desktopInstanceID(profile) != desktopInstanceID(alias) {
		t.Fatal("same profile with an equivalent path lost its desktop lock")
	}
	if runtime.GOOS == "windows" && desktopInstanceID(profile) != desktopInstanceID(AppPaths{RoamingRoot: strings.ToUpper(profile.RoamingRoot)}) {
		t.Fatal("Windows path casing created a duplicate desktop")
	}
}
