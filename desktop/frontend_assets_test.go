package main

import (
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"testing"
)

// Vite emits shared modules such as _commonjsHelpers-*.js. The default
// go:embed directory rule silently omits them, unlike Vite's preview server.
func TestPackagedFrontendContainsEntireViteBuild(t *testing.T) {
	count := 0
	err := filepath.WalkDir("frontend/dist", func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		count++
		built, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		packaged, err := assets.ReadFile(filepath.ToSlash(path))
		if err != nil {
			t.Errorf("production asset omitted from executable: %s: %v", path, err)
			return nil
		}
		if !bytes.Equal(built, packaged) {
			t.Errorf("production asset differs from Vite output: %s", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if count == 0 {
		t.Fatal("Vite production build is missing")
	}
}
