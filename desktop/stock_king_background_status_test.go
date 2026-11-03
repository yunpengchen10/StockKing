package main

import (
	"crypto/sha256"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStockKingBackgroundStatusOmitsLargeResearchResult(t *testing.T) {
	root := t.TempDir()
	app := &App{paths: AppPaths{DataDir: filepath.Join(root, "data"), ConfigDir: filepath.Join(root, "config")}}
	path := backgroundStatusPath(app.paths)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	// A nested historical result must be skipped, not copied into the Wails
	// response. This test never launches an engine, browser, or quote request.
	encoded := []byte(`{"status":"completed","updatedAt":"2026-11-03T14:55:00+08:00","slot":"1455","taskId":"saved-task","stage":"saved","message":"saved","error":"","enabled":false,"autoRecommendations":false,"result":{"controls":[{"raw":"` + strings.Repeat("evidence", 3*1024*1024) + `"}],"status":"nested-not-top-level"},"results":[{"also":"not displayed"}]}`)
	if err := os.WriteFile(path, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	before := sha256.Sum256(encoded)
	status := app.GetStockKingBackgroundStatus()
	for key, expected := range map[string]any{
		"status": "completed", "updatedAt": "2026-11-03T14:55:00+08:00", "slot": "1455",
		"taskId": "saved-task", "stage": "saved", "message": "saved", "enabled": true, "autoRecommendations": true,
	} {
		if status[key] != expected {
			t.Errorf("%s = %v, want %v", key, status[key], expected)
		}
	}
	for _, key := range []string{"result", "results", "controls"} {
		if _, exists := status[key]; exists {
			t.Fatalf("background status leaked archived %s", key)
		}
	}
	response, err := json.Marshal(status)
	if err != nil || len(response) > 1024 {
		t.Fatalf("status response is not compact: %d bytes, %v", len(response), err)
	}
	after, err := os.ReadFile(path)
	if err != nil || sha256.Sum256(after) != before {
		t.Fatal("reading status changed the original archive")
	}
}

func TestStockKingBackgroundStatusReadsLatestStateAndLiveFlags(t *testing.T) {
	root := t.TempDir()
	app := &App{paths: AppPaths{DataDir: filepath.Join(root, "data"), ConfigDir: filepath.Join(root, "config")}}
	if status := app.GetStockKingBackgroundStatus(); status["status"] != "never_run" || status["enabled"] != true {
		t.Fatalf("unexpected absent status: %v", status)
	}
	if err := os.MkdirAll(app.paths.ConfigDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(backgroundLearningFlagPath(app.paths), []byte("disabled"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeBackgroundStatus(app.paths, map[string]any{"status": "failed", "slot": "weekly", "message": "source unavailable", "error": "offline fixture"})
	status := app.GetStockKingBackgroundStatus()
	if status["status"] != "failed" || status["enabled"] != false || status["error"] != "offline fixture" || status["updatedAt"] == "" {
		t.Fatalf("lost failure metadata or current flag: %v", status)
	}
	writeBackgroundStatus(app.paths, map[string]any{"status": "completed", "slot": "1030"})
	status = app.GetStockKingBackgroundStatus()
	if status["status"] != "completed" || status["slot"] != "1030" || status["message"] != nil {
		t.Fatalf("old status persisted after replacement: %v", status)
	}
}
