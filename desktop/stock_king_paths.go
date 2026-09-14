package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// AppPaths keeps every writable Stock King artifact outside the installation
// directory. This deliberately does not reuse the legacy go-stock or Daily
// directories, so launching either old application cannot alter Stock King.
type AppPaths struct {
	RoamingRoot string `json:"roamingRoot"`
	LocalRoot   string `json:"localRoot"`
	DataDir     string `json:"dataDir"`
	ConfigDir   string `json:"configDir"`
	LogDir      string `json:"logDir"`
	CacheDir    string `json:"cacheDir"`
	ModelDir    string `json:"modelDir"`
}

func resolveAppPaths() (AppPaths, error) {
	roaming := os.Getenv("APPDATA")
	local := os.Getenv("LOCALAPPDATA")
	if roaming == "" {
		configDir, err := os.UserConfigDir()
		if err != nil {
			return AppPaths{}, err
		}
		roaming = configDir
	}
	if local == "" {
		cacheDir, err := os.UserCacheDir()
		if err != nil {
			return AppPaths{}, err
		}
		local = cacheDir
	}

	paths := AppPaths{
		RoamingRoot: filepath.Join(roaming, "Stock King"),
		LocalRoot:   filepath.Join(local, "Stock King"),
	}
	paths.DataDir = filepath.Join(paths.RoamingRoot, "data")
	paths.ConfigDir = filepath.Join(paths.RoamingRoot, "config")
	paths.LogDir = filepath.Join(paths.LocalRoot, "logs")
	paths.CacheDir = filepath.Join(paths.LocalRoot, "cache")
	paths.ModelDir = filepath.Join(paths.LocalRoot, "models")
	if runtime.GOOS == "darwin" {
		paths.ModelDir = filepath.Join(paths.RoamingRoot, "models")
	}

	for _, dir := range []string{
		paths.RoamingRoot, paths.LocalRoot, paths.DataDir, paths.ConfigDir,
		paths.LogDir, paths.CacheDir, paths.ModelDir,
	} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return AppPaths{}, fmt.Errorf("create Stock King directory %s: %w", dir, err)
		}
	}
	return paths, nil
}

func (p AppPaths) databaseDSN() string {
	dbPath := filepath.Join(p.DataDir, "stock-king.db")
	return filepath.ToSlash(dbPath) + "?_busy_timeout=10000&_journal_mode=WAL&_synchronous=NORMAL&_cache_size=-524288"
}
