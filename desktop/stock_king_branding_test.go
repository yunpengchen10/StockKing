package main

import (
	"bytes"
	"image/png"
	"os"
	"testing"
)

func TestPublicBrandImagesAreValidPNG(t *testing.T) {
	for _, path := range []string{"build/appicon.png", "frontend/src/assets/images/logo-universal.png", "../branding/stock-king-logo.png"} {
		t.Run(path, func(t *testing.T) {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := png.Decode(bytes.NewReader(data)); err != nil {
				t.Fatalf("public brand image cannot be decoded: %v", err)
			}
		})
	}
}
