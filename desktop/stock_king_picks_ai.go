package main

import (
	"fmt"
	"go-stock/backend/data"
	"strconv"
)

const kingPicksAIKey = "picks.ai.configId"

// A choice persists, but no platform or billable call is selected implicitly.
func (a *App) SetStockKingPicksAIConfig(id uint) error {
	settings := data.GetSettingConfig()
	if settings == nil {
		return fmt.Errorf("AI配置不可用")
	}
	selected, err := selectStockKingAIConfig(settings.AiConfigs, id)
	if err != nil {
		return err
	}
	_ = selected

	return a.SaveStockKingPreference(kingPicksAIKey, strconv.FormatUint(uint64(id), 10))
}

// This secret-bearing map is sent only to the authenticated loopback sidecar.
// It must never be returned across the frontend bridge or persisted in a run.
func (a *App) stockKingPicksAIConfig() (map[string]any, error) {
	id, _ := strconv.ParseUint(a.GetStockKingPreference(kingPicksAIKey), 10, 32)
	settings := data.GetSettingConfig()
	if settings == nil {
		return nil, fmt.Errorf("AI配置不可用")
	}
	selected, err := selectStockKingAIConfig(settings.AiConfigs, uint(id))
	if err != nil {
		return nil, err
	}
	return map[string]any{"name": selected.Name, "model": selected.ModelName,
		"api_key": selected.ApiKey, "base_url": selected.BaseUrl, "timeout": selected.TimeOut,
		"http_proxy": selected.HttpProxy, "http_proxy_enabled": selected.HttpProxyEnabled}, nil
}
