package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

type EngineStatus struct {
	State        string `json:"state"`
	Ready        bool   `json:"ready"`
	Port         int    `json:"port,omitempty"`
	StartedAt    string `json:"startedAt,omitempty"`
	LastCheckAt  string `json:"lastCheckAt,omitempty"`
	RestartCount int    `json:"restartCount"`
	Message      string `json:"message,omitempty"`
}

type SidecarManager struct {
	mu          sync.RWMutex
	paths       AppPaths
	status      EngineStatus
	token       string
	port        int
	cmd         *exec.Cmd
	cancel      context.CancelFunc
	done        chan struct{}
	client      *http.Client
	stopping    bool
	marketURL   string
	marketToken string
}

func NewSidecarManager(paths AppPaths) *SidecarManager {
	return &SidecarManager{
		paths:  paths,
		status: EngineStatus{State: "stopped", Message: "Stock King engine has not started"},
		// Full-market screening and manual model training are intentionally
		// long-running. Wails contexts still allow cancellation at shutdown.
		client: &http.Client{Timeout: 30 * time.Minute},
	}
}

func (m *SidecarManager) Start(parent context.Context) {
	m.mu.Lock()
	if m.cancel != nil {
		m.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(parent)
	m.cancel = cancel
	m.done = make(chan struct{})
	done := m.done
	m.stopping = false
	m.status = EngineStatus{State: "starting", Message: "Starting Stock King research engine"}
	m.mu.Unlock()
	go func() {
		defer close(done)
		m.supervise(ctx)
	}()
}

func (m *SidecarManager) Stop() {
	m.mu.Lock()
	m.stopping = true
	cancel := m.cancel
	done := m.done
	cmd := m.cmd
	m.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
	// Reap the helper before Wails exits. Keep Start disabled until the old
	// supervisor has finished, including cancellation during initial launch.
	if done != nil {
		<-done
	}
	m.mu.Lock()
	m.cancel = nil
	m.cmd = nil
	m.done = nil
	m.mu.Unlock()
	m.setStatus(EngineStatus{State: "stopped", Message: "Stock King engine stopped"})
}

func (m *SidecarManager) Status() EngineStatus {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.status
}

func (m *SidecarManager) supervise(ctx context.Context) {
	marketURL, marketToken, err := startMarketGateway(ctx)
	if err != nil {
		m.setStatus(EngineStatus{State: "unavailable", Message: "行情接口启动失败: " + err.Error()})
		return
	}
	m.marketURL, m.marketToken = marketURL, marketToken
	restarts := 0
	for {
		if ctx.Err() != nil {
			return
		}
		cmd, err := m.launch(ctx, restarts)
		if err != nil {
			m.setStatus(EngineStatus{State: "unavailable", RestartCount: restarts, Message: err.Error()})
			if !waitContext(ctx, 3*time.Second) {
				return
			}
			restarts++
			continue
		}

		waitCh := make(chan error, 1)
		go func(process *exec.Cmd) { waitCh <- process.Wait() }(cmd)
		ready := false
		deadline := time.NewTimer(60 * time.Second)
		ticker := time.NewTicker(350 * time.Millisecond)
		for !ready {
			select {
			case <-ctx.Done():
				deadline.Stop()
				ticker.Stop()
				_ = cmd.Process.Kill()
				<-waitCh
				return
			case err := <-waitCh:
				deadline.Stop()
				ticker.Stop()
				m.setStatus(EngineStatus{State: "restarting", RestartCount: restarts, Message: processExitMessage(err)})
				ready = false
				cmd = nil
			case <-ticker.C:
				if m.healthCheck(ctx) == nil {
					ready = true
				}
			case <-deadline.C:
				ticker.Stop()
				_ = cmd.Process.Kill()
				<-waitCh
				m.setStatus(EngineStatus{State: "restarting", RestartCount: restarts, Message: "Stock King engine health check timed out"})
				cmd = nil
			}
			if cmd == nil {
				break
			}
		}
		if cmd == nil {
			restarts++
			if !waitContext(ctx, 2*time.Second) {
				return
			}
			continue
		}
		deadline.Stop()
		ticker.Stop()
		now := time.Now().Format(time.RFC3339)
		m.mu.Lock()
		if !m.stopping {
			m.status = EngineStatus{State: "ready", Ready: true, Port: m.port, StartedAt: now, LastCheckAt: now, RestartCount: restarts}
		}
		m.mu.Unlock()

		select {
		case <-ctx.Done():
			_ = cmd.Process.Kill()
			<-waitCh
			return
		case err := <-waitCh:
			m.setStatus(EngineStatus{State: "restarting", RestartCount: restarts + 1, Message: processExitMessage(err)})
			restarts++
			if !waitContext(ctx, 2*time.Second) {
				return
			}
		}
	}
}

func (m *SidecarManager) launch(ctx context.Context, restarts int) (*exec.Cmd, error) {
	port, err := freeLocalPort()
	if err != nil {
		return nil, err
	}
	token, err := randomToken()
	if err != nil {
		return nil, err
	}

	command, args, workingDir, err := discoverDailyCommand(port)
	if err != nil {
		return nil, err
	}
	cmd := exec.CommandContext(ctx, command, args...)
	cmd.WaitDelay = 3 * time.Second
	cmd.Dir = workingDir
	hideSidecarWindow(cmd)
	dailyData := filepath.Join(m.paths.DataDir, "daily")
	dailyLogs := filepath.Join(m.paths.LogDir, "daily")
	screeningCache := filepath.Join(m.paths.CacheDir, "screening")
	adaptiveData := filepath.Join(dailyData, "king-adaptive")
	for _, dir := range []string{dailyData, dailyLogs, screeningCache, adaptiveData} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	cmd.Env = append(os.Environ(),
		"STOCK_KING_MARKET_URL="+m.marketURL,
		"STOCK_KING_MARKET_TOKEN="+m.marketToken,
		"STOCK_KING_SIDECAR_TOKEN="+token,
		"DATABASE_PATH="+filepath.Join(dailyData, "stock-analysis.db"),
		"LOG_DIR="+dailyLogs,
		"SCREENING_DATA_DIR="+screeningCache,
		"YAO_SCOUT_DATA_DIR="+adaptiveData,
		"MODEL_DIR="+m.paths.ModelDir,
		"WEBUI_HOST=127.0.0.1",
		"WEBUI_PORT="+strconv.Itoa(port),
		"CORS_ALLOW_ALL=false",
		"SCREENING_ENABLED=true",
	)
	logFile, err := os.OpenFile(filepath.Join(m.paths.LogDir, "daily-sidecar.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err == nil {
		cmd.Stdout = logFile
		cmd.Stderr = logFile
	}
	if err := cmd.Start(); err != nil {
		if logFile != nil {
			_ = logFile.Close()
		}
		return nil, fmt.Errorf("start Stock King engine: %w", err)
	}
	// Start duplicates the log handles into the child. The parent must not
	// retain an open file on every engine restart.
	if logFile != nil {
		_ = logFile.Close()
	}
	m.mu.Lock()
	m.port = port
	m.token = token
	m.cmd = cmd
	if !m.stopping {
		m.status = EngineStatus{State: "starting", Port: port, RestartCount: restarts, Message: "Waiting for Stock King engine health check"}
	}
	m.mu.Unlock()
	return cmd, nil
}

func (m *SidecarManager) healthCheck(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, m.baseURL()+"/api/v1/health", nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-Stock-King-Token", m.accessToken())
	resp, err := m.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("health status %d", resp.StatusCode)
	}
	return nil
}

func (m *SidecarManager) Request(ctx context.Context, method, path string, input any, output any) error {
	status := m.Status()
	if !status.Ready {
		return fmt.Errorf("Stock King engine is not ready: %s", status.Message)
	}
	var body io.Reader
	if input != nil {
		payload, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(payload)
	}
	req, err := http.NewRequestWithContext(ctx, method, m.baseURL()+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("X-Stock-King-Token", m.accessToken())
	if input != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := m.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Stock King API %s %s returned %d: %s", method, path, resp.StatusCode, strings.TrimSpace(string(payload)))
	}
	if output == nil || len(payload) == 0 {
		return nil
	}
	return json.Unmarshal(payload, output)
}

func (m *SidecarManager) baseURL() string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return fmt.Sprintf("http://127.0.0.1:%d", m.port)
}

func (m *SidecarManager) accessToken() string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.token
}

func (m *SidecarManager) setStatus(status EngineStatus) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.stopping && status.State != "stopped" {
		return
	}
	if status.Port == 0 {
		status.Port = m.port
	}
	if status.RestartCount == 0 {
		status.RestartCount = m.status.RestartCount
	}
	m.status = status
}

func randomToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("generate sidecar token: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

func freeLocalPort() (int, error) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}

func discoverDailyCommand(port int) (string, []string, string, error) {
	exe, _ := os.Executable()
	cwd, _ := os.Getwd()
	roots := []string{filepath.Dir(exe), cwd}
	for _, root := range append([]string{}, roots...) {
		roots = append(roots, filepath.Dir(root), filepath.Dir(filepath.Dir(root)))
	}
	exeNames := []string{"stock_analysis.exe", "stock-analysis.exe"}
	if runtime.GOOS != "windows" {
		exeNames = []string{"stock_analysis", "stock-analysis"}
	}
	for _, root := range uniquePaths(roots) {
		for _, rel := range []string{"Resources/daily-engine/stock_analysis", "resources/daily-engine/stock_analysis", "daily-engine/dist/backend/stock_analysis", "daily-engine/stock_analysis"} {
			for _, name := range exeNames {
				candidate := filepath.Join(root, filepath.FromSlash(rel), name)
				if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
					return candidate, []string{"--serve-only", "--host", "127.0.0.1", "--port", strconv.Itoa(port)}, filepath.Dir(candidate), nil
				}
			}
		}
	}
	for _, root := range uniquePaths(roots) {
		for _, rel := range []string{"daily-engine", "../daily-engine"} {
			dailyRoot, _ := filepath.Abs(filepath.Join(root, rel))
			if info, err := os.Stat(filepath.Join(dailyRoot, "main.py")); err == nil && !info.IsDir() {
				python, pythonErr := discoverPython(uniquePaths(append(roots, dailyRoot, filepath.Dir(dailyRoot))))
				if pythonErr != nil {
					return "", nil, "", pythonErr
				}
				return python, []string{"main.py", "--serve-only", "--host", "127.0.0.1", "--port", strconv.Itoa(port)}, dailyRoot, nil
			}
		}
	}
	return "", nil, "", errors.New("Stock King engine entry point was not found")
}

func discoverPython(roots []string) (string, error) {
	if configured := strings.TrimSpace(os.Getenv("STOCK_KING_PYTHON")); configured != "" {
		if info, err := os.Stat(configured); err == nil && !info.IsDir() {
			return configured, nil
		}
		return "", fmt.Errorf("STOCK_KING_PYTHON does not exist: %s", configured)
	}
	for _, root := range roots {
		candidates := []string{
			filepath.Join(root, ".venv", "Scripts", "python.exe"),
			filepath.Join(root, ".venv", "bin", "python"),
		}
		for _, candidate := range candidates {
			if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
				return candidate, nil
			}
		}
	}
	for _, command := range []string{"python3", "python", "python.exe"} {
		if python, err := exec.LookPath(command); err == nil {
			return python, nil
		}
	}
	return "", errors.New("Stock King engine was not packaged and Python was not found")
}

func uniquePaths(paths []string) []string {
	seen := make(map[string]struct{}, len(paths))
	result := make([]string, 0, len(paths))
	for _, path := range paths {
		path = filepath.Clean(path)
		if _, exists := seen[path]; path == "." || exists {
			continue
		}
		seen[path] = struct{}{}
		result = append(result, path)
	}
	return result
}

func waitContext(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func processExitMessage(err error) string {
	if err == nil {
		return "Stock King engine exited"
	}
	return "Stock King engine exited: " + err.Error()
}
