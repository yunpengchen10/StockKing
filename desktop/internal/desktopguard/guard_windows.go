//go:build windows

package desktopguard

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	user32                   = windows.NewLazySystemDLL("user32.dll")
	enumWindows              = user32.NewProc("EnumWindows")
	getWindowThreadProcessID = user32.NewProc("GetWindowThreadProcessId")
	getClassName             = user32.NewProc("GetClassNameW")
	isIconic                 = user32.NewProc("IsIconic")
	getSystemMetrics         = user32.NewProc("GetSystemMetrics")
)

func Run(logDir string) (handled bool, exitCode int) {
	if hasChildArgument(os.Args[1:]) {
		return false, 0
	}
	if err := os.MkdirAll(logDir, 0700); err != nil {
		return true, 1
	}
	evidence := &rotatingWriter{path: filepath.Join(logDir, "desktop-lifecycle.log")}
	crashes := &rotatingWriter{path: filepath.Join(logDir, "desktop-crash.log")}
	executable, err := os.Executable()
	if err != nil {
		lifecycle(evidence, "resolve executable: %v", err)
		return true, 1
	}
	workingDir, err := os.Getwd()
	if err != nil {
		workingDir = filepath.Dir(executable)
	}
	shutdown := watchSessionShutdown()
	defer shutdown.close()
	var budget restartBudget
	minimized := false
	for {
		if shutdown.active() {
			return true, 0
		}
		session, err := os.MkdirTemp(logDir, ".desktop-session-")
		if err != nil {
			lifecycle(evidence, "create session: %v", err)
			return true, 1
		}
		child, err := startChild(executable, childArguments(os.Args[1:]), workingDir, childEnvironment(os.Environ(), session, minimized), crashes)
		if err != nil {
			removeSession(session)
			lifecycle(evidence, "start desktop: %v", err)
			return true, 1
		}
		lifecycle(evidence, "desktop started pid=%d minimized=%t supervisor=%d", child.pid, minimized, os.Getpid())
		code, lastMinimized, err := child.wait(minimized)
		minimized = lastMinimized
		ready := exists(filepath.Join(session, "ready"))
		closing := exists(filepath.Join(session, "closing"))
		child.close()
		removeSession(session)
		stopping := shutdown.active()
		lifecycle(evidence, "desktop stopped pid=%d exit=%d ready=%t closing=%t shutdown=%t minimized=%t waitError=%v", child.pid, code, ready, closing, stopping, minimized, err)
		if err != nil {
			return true, 1
		}
		if !shouldRestart(code, ready, closing, stopping) {
			return true, int(code)
		}
		delay, allowed := budget.next(time.Now())
		if !allowed {
			lifecycle(evidence, "automatic recovery stopped: three recoveries within two minutes")
			return true, int(code)
		}
		lifecycle(evidence, "recovering desktop after unexpected exit in %s", delay)
		deadline := time.Now().Add(delay)
		for time.Now().Before(deadline) {
			if shutdown.active() {
				return true, 0
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
}

func removeSession(path string) {
	_ = os.Remove(filepath.Join(path, "ready"))
	_ = os.Remove(filepath.Join(path, "closing"))
	_ = os.Remove(path)
}

type childProcess struct {
	process windows.Handle
	job     windows.Handle
	pid     uint32
	output  *os.File
	drained chan struct{}
}

// The child cannot run any code until it belongs to our kill-on-close job. This
// removes the race where an engine could start before ownership was established.
// The job handle is not inherited; only the supervisor can keep the job alive.
func startChild(executable string, args []string, workingDir string, env []string, output io.Writer) (_ *childProcess, resultErr error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return nil, fmt.Errorf("create child job: %w", err)
	}
	defer func() {
		if resultErr != nil {
			_ = windows.CloseHandle(job)
		}
	}()
	limits := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	limits.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&limits)), uint32(unsafe.Sizeof(limits))); err != nil {
		return nil, fmt.Errorf("configure child job: %w", err)
	}
	readPipe, writePipe, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	defer writePipe.Close()
	defer func() {
		if resultErr != nil {
			_ = readPipe.Close()
		}
	}()
	input, err := os.OpenFile("NUL", os.O_RDONLY, 0)
	if err != nil {
		return nil, err
	}
	defer input.Close()
	handles := []windows.Handle{windows.Handle(input.Fd()), windows.Handle(writePipe.Fd())}
	for _, handle := range handles {
		if err := windows.SetHandleInformation(handle, windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT); err != nil {
			return nil, err
		}
	}
	attributes, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		return nil, err
	}
	defer attributes.Delete()
	if err := attributes.Update(windows.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, unsafe.Pointer(&handles[0]), uintptr(len(handles))*unsafe.Sizeof(handles[0])); err != nil {
		return nil, err
	}
	startup := windows.StartupInfoEx{}
	startup.Cb = uint32(unsafe.Sizeof(startup))
	startup.Flags = windows.STARTF_USESTDHANDLES
	startup.StdInput, startup.StdOutput, startup.StdErr = handles[0], handles[1], handles[1]
	startup.ProcThreadAttributeList = attributes.List()
	app, err := windows.UTF16PtrFromString(executable)
	if err != nil {
		return nil, err
	}
	commandArgs := append([]string{executable}, args...)
	for index, arg := range commandArgs {
		commandArgs[index] = syscall.EscapeArg(arg)
	}
	commandLine, err := windows.UTF16FromString(strings.Join(commandArgs, " "))
	if err != nil {
		return nil, err
	}
	directory, err := windows.UTF16PtrFromString(workingDir)
	if err != nil {
		return nil, err
	}
	environment := environmentBlock(env)
	var process windows.ProcessInformation
	flags := uint32(windows.CREATE_SUSPENDED | windows.CREATE_UNICODE_ENVIRONMENT | windows.EXTENDED_STARTUPINFO_PRESENT | windows.CREATE_NO_WINDOW)
	if err := windows.CreateProcess(app, &commandLine[0], nil, nil, true, flags, &environment[0], directory, &startup.StartupInfo, &process); err != nil {
		return nil, fmt.Errorf("create desktop process: %w", err)
	}
	runtime.KeepAlive(handles)
	defer windows.CloseHandle(process.Thread)
	defer func() {
		if resultErr != nil {
			_ = windows.TerminateProcess(process.Process, 1)
			_, _ = windows.WaitForSingleObject(process.Process, 5000)
			_ = windows.CloseHandle(process.Process)
		}
	}()
	if err := windows.AssignProcessToJobObject(job, process.Process); err != nil {
		return nil, fmt.Errorf("assign desktop to owned job: %w", err)
	}
	if _, err := windows.ResumeThread(process.Thread); err != nil {
		return nil, fmt.Errorf("resume desktop: %w", err)
	}
	child := &childProcess{process: process.Process, job: job, pid: process.ProcessId, output: readPipe, drained: make(chan struct{})}
	go func() { _, _ = io.Copy(drainWriter{destination: output}, readPipe); close(child.drained) }()
	return child, nil
}

func environmentBlock(env []string) []uint16 {
	items := append([]string(nil), env...)
	sort.SliceStable(items, func(i, j int) bool { return strings.ToUpper(items[i]) < strings.ToUpper(items[j]) })
	block := utf16.Encode([]rune(strings.Join(items, "\x00") + "\x00\x00"))
	return block
}

func (p *childProcess) wait(minimized bool) (uint32, bool, error) {
	for {
		result, err := windows.WaitForSingleObject(p.process, 250)
		if err != nil {
			return 1, minimized, err
		}
		if result == windows.WAIT_OBJECT_0 {
			var code uint32
			err := windows.GetExitCodeProcess(p.process, &code)
			return code, minimized, err
		}
		if result != uint32(windows.WAIT_TIMEOUT) {
			return 1, minimized, fmt.Errorf("unexpected process wait result: %d", result)
		}
		if state, found := minimizedWindow(p.pid); found {
			minimized = state
		}
	}
}

func (p *childProcess) close() {
	// Terminate and drain this exact job before launching a replacement, so the
	// replacement cannot join an old WebView environment that is still exiting.
	// The kill-on-close policy also covers abrupt supervisor termination.
	_ = windows.TerminateJobObject(p.job, 1)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		var accounting struct {
			totalUser, totalKernel, periodUser, periodKernel                 int64
			pageFaults, totalProcesses, activeProcesses, terminatedProcesses uint32
		}
		err := windows.QueryInformationJobObject(p.job, windows.JobObjectBasicAccountingInformation, uintptr(unsafe.Pointer(&accounting)), uint32(unsafe.Sizeof(accounting)), nil)
		if err != nil || accounting.activeProcesses == 0 {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	_ = windows.CloseHandle(p.job)
	_ = windows.CloseHandle(p.process)
	select {
	case <-p.drained:
	case <-time.After(2 * time.Second):
	}
	_ = p.output.Close()
}

type windowLookup struct {
	pid              uint32
	minimized, found bool
}

var windowLookups sync.Map
var windowLookupSequence atomic.Uintptr

// Windows callback trampolines are never reclaimed by Go. Allocate one for all
// polls rather than a closure per poll, which would exhaust them during idle.
var enumerateChildWindow = windows.NewCallback(func(hwnd, parameter uintptr) uintptr {
	lookupValue, exists := windowLookups.Load(parameter)
	if !exists {
		return 0
	}
	lookup := lookupValue.(*windowLookup)
	var owner uint32
	_, _, _ = getWindowThreadProcessID.Call(hwnd, uintptr(unsafe.Pointer(&owner)))
	if owner != lookup.pid {
		return 1
	}
	var name [128]uint16
	length, _, _ := getClassName.Call(hwnd, uintptr(unsafe.Pointer(&name[0])), uintptr(len(name)))
	if length == 0 || windows.UTF16ToString(name[:]) != "wailsWindow" {
		return 1
	}
	value, _, _ := isIconic.Call(hwnd)
	lookup.minimized, lookup.found = value != 0, true
	return 0
})

func minimizedWindow(pid uint32) (minimized bool, found bool) {
	lookup := windowLookup{pid: pid}
	key := windowLookupSequence.Add(1)
	windowLookups.Store(key, &lookup)
	_, _, _ = enumWindows.Call(enumerateChildWindow, key)
	windowLookups.Delete(key)
	return lookup.minimized, lookup.found
}
