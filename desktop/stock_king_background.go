package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const backgroundTaskArgument = "--stock-king-task="

func requestedBackgroundTask(args []string) string {
	for _, arg := range args {
		if strings.HasPrefix(strings.ToLower(strings.TrimSpace(arg)), backgroundTaskArgument) {
			return strings.TrimSpace(strings.TrimPrefix(strings.ToLower(strings.TrimSpace(arg)), backgroundTaskArgument))
		}
	}
	return ""
}

func backgroundLearningFlagPath(paths AppPaths) string {
	return filepath.Join(paths.ConfigDir, "background-learning.disabled")
}

func backgroundLearningEnabled(paths AppPaths) bool {
	_, err := os.Stat(backgroundLearningFlagPath(paths))
	return os.IsNotExist(err)
}

func backgroundStatusPath(paths AppPaths) string {
	return filepath.Join(paths.DataDir, "daily", "king-adaptive", "background-status.json")
}

func writeBackgroundStatus(paths AppPaths, payload map[string]any) {
	payload["updatedAt"] = time.Now().Format(time.RFC3339)
	path := backgroundStatusPath(paths)
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	encoded, err := json.MarshalIndent(payload, "", "  ")
	if err == nil {
		_ = os.WriteFile(path, encoded, 0o600)
	}
}

func runStockKingBackgroundTask(paths AppPaths, slot string) int {
	allowed := map[string]bool{"0920": true, "0922": true, "0925": true, "1030": true, "1455": true, "review": true, "weekly": true}
	if !allowed[slot] {
		writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": "invalid_task"})
		return 2
	}
	if !backgroundLearningEnabled(paths) {
		writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": "disabled_by_user"})
		return 0
	}

	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Minute)
	defer cancel()
	manager := NewSidecarManager(paths)
	manager.Start(ctx)
	defer manager.Stop()
	deadline := time.Now().Add(90 * time.Second)
	for time.Now().Before(deadline) && !manager.Status().Ready {
		time.Sleep(500 * time.Millisecond)
	}
	if !manager.Status().Ready {
		writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": "engine_unavailable", "message": manager.Status().Message})
		return 3
	}

	var accepted map[string]any
	err := manager.Request(ctx, "POST", "/api/v1/stock-king/picks/runs", map[string]any{
		"scan_slot": slot,
		"top_n":     5,
	}, &accepted)
	if err != nil {
		writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": "submit_failed", "message": err.Error()})
		return 4
	}
	taskID := strings.TrimSpace(fmt.Sprint(accepted["task_id"]))
	if taskID == "" {
		writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": "submit_failed", "message": "missing task id"})
		return 4
	}
	for {
		select {
		case <-ctx.Done():
			writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": "timeout", "taskId": taskID})
			return 5
		case <-time.After(2 * time.Second):
			var status map[string]any
			if err := manager.Request(ctx, "GET", "/api/v1/stock-king/picks/tasks/"+taskID, nil, &status); err != nil {
				continue
			}
			state := strings.TrimSpace(fmt.Sprint(status["status"]))
			if state == "completed" {
				writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": "completed", "taskId": taskID, "result": status["result"]})
				return 0
			}
			if state == "failed" || state == "cancelled" {
				writeBackgroundStatus(paths, map[string]any{"slot": slot, "status": state, "taskId": taskID, "message": status["error"]})
				return 6
			}
		}
	}
}

func (a *App) GetStockKingBackgroundLearning() bool {
	return backgroundLearningEnabled(a.paths)
}

func (a *App) SetStockKingBackgroundLearning(enabled bool) error {
	path := backgroundLearningFlagPath(a.paths)
	if enabled {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
	} else {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(path, []byte("disabled by user\n"), 0o600); err != nil {
			return err
		}
	}
	return a.SaveStockKingPreference("background.learning.enabled", fmt.Sprint(enabled))
}

func (a *App) GetStockKingBackgroundStatus() map[string]any {
	payload := map[string]any{"status": "never_run", "enabled": backgroundLearningEnabled(a.paths)}
	encoded, err := os.ReadFile(backgroundStatusPath(a.paths))
	if err == nil {
		_ = json.Unmarshal(encoded, &payload)
	}
	payload["enabled"] = backgroundLearningEnabled(a.paths)
	return payload
}
