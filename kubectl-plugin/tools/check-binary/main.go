// check-binary exercises the shipped executable, not imported plugin functions.
// Its recording kubectl fixture never contacts a cluster or evaluates arguments.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"time"
)

type recordedExec struct {
	Args       []string `json:"args"`
	Kubeconfig string   `json:"kubeconfig"`
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}

func require(ok bool, message string) {
	if !ok {
		panic(message)
	}
}

func copyExecutable(source, destination string) {
	in, err := os.Open(source)
	must(err)
	defer in.Close()
	out, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o700)
	must(err)
	_, err = io.Copy(out, in)
	must(err)
	must(out.Close())
}

func fixture() int {
	log, err := os.OpenFile(os.Getenv("CHECK_LOG"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	must(err)
	must(json.NewEncoder(log).Encode(os.Args[1:]))
	must(log.Close())
	args := os.Args[1:]
	if len(args) >= 2 && args[0] == "get" {
		mode := os.Getenv("CHECK_DISCOVERY")
		if args[1] == "deployment" {
			if mode == "" {
				return 0
			}
			return 1
		}
		if args[1] == "deployments" {
			switch mode {
			case "single":
				fmt.Println(`{"items":[{"metadata":{"name":"selected-holmes"}}]}`)
			case "multiple":
				fmt.Println(`{"items":[{"metadata":{"name":"one"}},{"metadata":{"name":"two"}}]}`)
			case "invalid":
				fmt.Println("not json")
			case "denied":
				return 1
			default:
				fmt.Println(`{"items":[]}`)
			}
			return 0
		}
	}
	if len(args) > 0 && args[0] == "exec" {
		must(json.NewEncoder(os.Stdout).Encode(recordedExec{args, os.Getenv("KUBECONFIG")}))
		code, _ := strconv.Atoi(os.Getenv("CHECK_EXIT"))
		return code
	}
	return 99
}

func main() {
	if strings.TrimSuffix(filepath.Base(os.Args[0]), ".exe") == "kubectl" {
		os.Exit(fixture())
	}
	binaryFlag := flag.String("binary", "", "built native plugin to verify")
	kubectlFlag := flag.String("kubectl", "", "real kubectl executable for offline plugin dispatch")
	flag.Parse()
	require(*binaryFlag != "" && *kubectlFlag != "", "--binary and --kubectl are required")
	binary, err := filepath.Abs(*binaryFlag)
	must(err)
	realKubectl, err := exec.LookPath(*kubectlFlag)
	must(err)
	realKubectl, err = filepath.Abs(realKubectl)
	must(err)
	self, err := os.Executable()
	must(err)
	dir, err := os.MkdirTemp("", "investigate binary checks ")
	must(err)
	defer os.RemoveAll(dir)
	suffix := ""
	if runtime.GOOS == "windows" {
		suffix = ".exe"
	}
	copyExecutable(self, filepath.Join(dir, "kubectl"+suffix))
	copyExecutable(binary, filepath.Join(dir, "kubectl-investigate"+suffix))
	callLog := filepath.Join(dir, "calls.jsonl")
	missing := filepath.Join(dir, "empty-path")
	must(os.Mkdir(missing, 0o700))

	// Do not inherit user's targeting, credentials, or auth plugins. The real
	// kubectl is invoked only for offline discovery/dispatch; all plugin calls
	// resolve to the recording fixture through PATH.
	baseEnv := []string{}
	for _, entry := range os.Environ() {
		key := strings.ToUpper(strings.SplitN(entry, "=", 2)[0])
		if key != "PATH" && key != "KUBECONFIG" && !strings.HasPrefix(key, "INVESTIGATOR_") &&
			!strings.HasPrefix(key, "BRAINTRUST_") && !strings.HasPrefix(key, "CHECK_") {
			baseEnv = append(baseEnv, entry)
		}
	}
	baseEnv = append(baseEnv, "KUBECONFIG="+filepath.Join(dir, "no-cluster-config"), "CHECK_LOG="+callLog)
	invoke := func(program string, args []string, overrides map[string]string) (int, string, string, [][]string) {
		must(os.WriteFile(callLog, nil, 0o600))
		env := append([]string{}, baseEnv...)
		searchPath := dir
		for key, value := range overrides {
			if key == "PATH" {
				searchPath = value
			} else {
				env = append(env, key+"="+value)
			}
		}
		env = append(env, "PATH="+searchPath)
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, program, args...)
		cmd.Env, cmd.Dir = env, dir
		var stdout, stderr strings.Builder
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
		err := cmd.Run()
		require(ctx.Err() == nil, "binary check timed out")
		code := 0
		if err != nil {
			exit, ok := err.(*exec.ExitError)
			require(ok, "could not execute binary")
			code = exit.ExitCode()
		}
		data, err := os.ReadFile(callLog)
		must(err)
		var calls [][]string
		for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
			if line != "" {
				var args []string
				must(json.Unmarshal([]byte(line), &args))
				calls = append(calls, args)
			}
		}
		return code, stdout.String(), stderr.String(), calls
	}

	question := "how many pods? quotes \"x\", 'y', $HOME; $(echo nope) & | < > `tick` 雪"
	args := []string{question, "--model", "saturn-vllm", "--custom", "", `C:\path with spaces\`}
	expected := append([]string{"exec", "-n", "aks-investigator", "deploy/aks-investigator-holmes", "--", "env", "HOME=/tmp", "python", "holmes_cli.py", "ask"}, args...)
	expected = append(expected, "--no-interactive")
	checkExec := func(stdout string, want []string) {
		var received recordedExec
		must(json.Unmarshal([]byte(stdout), &received))
		require(reflect.DeepEqual(received.Args, want), "exec argument vector mismatch")
		require(received.Kubeconfig == filepath.Join(dir, "no-cluster-config"), "kubeconfig changed")
	}
	code, _, stderr, calls := invoke(binary, nil, nil)
	require(code == 64 && strings.Contains(stderr, "Usage:") && len(calls) == 0, "usage check failed")
	for _, help := range []string{"--help", "-h"} {
		// Help must work with no kubectl on PATH, no kubeconfig, and no trace
		// credentials, even if a trace option is also supplied after help.
		for _, helpArgs := range [][]string{{help}, {help, "--trace", "braintrust"}} {
			code, stdout, stderr, calls := invoke(binary, helpArgs, map[string]string{"PATH": missing, "INVESTIGATOR_TIMING": "1"})
			require(code == 0 && strings.Contains(stdout, "Usage:") && strings.Contains(stdout, "--help") && stderr == "" && len(calls) == 0, "help must succeed locally on stdout")
		}
		code, stdout, stderr, calls := invoke(realKubectl, []string{"investigate", help}, nil)
		require(code == 0 && strings.Contains(stdout, "Usage:") && stderr == "" && len(calls) == 0, "kubectl-dispatched help must not contact the cluster")
		code, stdout, _, calls = invoke(binary, []string{question, help}, nil)
		require(code == 0 && len(calls) == 2, "trailing help must still be forwarded")
		want := append(append([]string{}, expected[:10]...), question, help, "--no-interactive")
		checkExec(stdout, want)
	}
	code, _, _, calls = invoke(binary, []string{question}, map[string]string{"PATH": missing})
	require(code == 127 && len(calls) == 0, "missing kubectl check failed")
	for _, trace := range [][]string{{"--trace", "braintrust"}, {"--trace=braintrust"}} {
		code, _, _, calls = invoke(binary, append([]string{question}, trace...), nil)
		require(code == 64 && len(calls) == 0, "trace credentials must fail before kubectl access")
	}
	code, stdout, _, calls := invoke(binary, args, map[string]string{"BRAINTRUST_API_KEY": "not-a-real-key", "BRAINTRUST_ORG": "fixture"})
	require(code == 0 && len(calls) == 2, "release discovery failed")
	checkExec(stdout, expected) // no -it with redirected stdin/stdout, no unrequested keys
	for _, mode := range []string{"-i", "--interactive", "-n", "--no-interactive"} {
		code, stdout, _, _ = invoke(binary, []string{question, mode}, nil)
		require(code == 0, "explicit mode failed")
		want := append(append([]string{}, expected[:10]...), question, mode)
		checkExec(stdout, want)
	}
	for _, trace := range [][]string{{"--trace", "braintrust"}, {"--trace=braintrust"}} {
		traceArgs := append([]string{question}, trace...)
		code, stdout, _, _ = invoke(binary, traceArgs, map[string]string{"BRAINTRUST_API_KEY": "not-a-real-key", "BRAINTRUST_ORG": "fixture"})
		require(code == 0, "trace invocation failed")
		want := append(append([]string{}, expected[:7]...), "BRAINTRUST_API_KEY=not-a-real-key", "BRAINTRUST_ORG=fixture", "python", "holmes_cli.py", "ask")
		want = append(want, traceArgs...)
		checkExec(stdout, append(want, "--no-interactive"))
	}
	for _, trace := range [][]string{{"--trace", "other"}, {"--trace=braintrust-other"}, {"--", "--trace=braintrust"}} {
		code, stdout, _, _ = invoke(binary, append([]string{question}, trace...), nil)
		require(code == 0 && !strings.Contains(stdout, "BRAINTRUST_API_KEY="), "trace matching was not exact")
	}
	code, stdout, _, calls = invoke(binary, []string{question}, map[string]string{"INVESTIGATOR_NAMESPACE": "custom", "INVESTIGATOR_DEPLOY": "explicit", "INVESTIGATOR_HOME": "/tmp/custom home"})
	require(code == 0 && len(calls) == 1, "explicit deployment should skip discovery")
	checkExec(stdout, []string{"exec", "-n", "custom", "deploy/explicit", "--", "env", "HOME=/tmp/custom home", "python", "holmes_cli.py", "ask", question, "--no-interactive"})
	code, stdout, _, calls = invoke(binary, []string{question}, map[string]string{"CHECK_DISCOVERY": "single", "INVESTIGATOR_RELEASE": "custom", "INVESTIGATOR_SELECTOR": "role=holmes"})
	require(code == 0 && len(calls) == 3 && calls[0][4] == "custom-holmes" && calls[1][5] == "role=holmes", "selector discovery failed")
	require(strings.Contains(stdout, "deploy/selected-holmes"), "wrong fallback deployment")
	for _, mode := range []string{"none", "multiple", "invalid", "denied"} {
		code, _, _, calls = invoke(binary, []string{question}, map[string]string{"CHECK_DISCOVERY": mode})
		require(code == 1 && len(calls) == 2, "failed discovery must not exec")
	}
	code, stdout, stderr, _ = invoke(binary, []string{question}, map[string]string{"INVESTIGATOR_TIMING": "1"})
	require(code == 0 && strings.Contains(stderr, "__AKSI_TIMING_EXEC_REQUEST__"), "local timing marker missing")
	timing := append(append([]string{}, expected[:5]...), "bash", "-c", `printf '__AKSI_TIMING_EXEC_STARTED__\n' >&2; exec "$@"`, "investigate-timing")
	timing = append(timing, "env", "HOME=/tmp", "python", "holmes_cli.py", "ask", question, "--no-interactive")
	checkExec(stdout, timing)
	code, _, _, _ = invoke(binary, []string{question}, map[string]string{"CHECK_EXIT": "23"})
	require(code == 23, "child exit code was not preserved")
	code, stdout, stderr, _ = invoke(realKubectl, []string{"plugin", "list"}, nil)
	require(code == 0 && strings.Contains(stdout+stderr, "kubectl-investigate"+suffix), "real kubectl discovery failed")
	code, stdout, _, _ = invoke(realKubectl, append([]string{"investigate"}, args...), nil)
	require(code == 0, "real kubectl plugin dispatch failed")
	checkExec(stdout, expected)
	code, _, _, _ = invoke(realKubectl, []string{"investigate", question}, map[string]string{"CHECK_EXIT": "23"})
	require(code != 0, "real kubectl hid the plugin failure")

	// Place a digest beside the exact executable that passed, for E2E download.
	data, err := os.ReadFile(binary)
	must(err)
	digest := fmt.Sprintf("%x  %s\n", sha256.Sum256(data), filepath.Base(binary))
	must(os.WriteFile(filepath.Join(filepath.Dir(binary), "SHA256SUMS"), []byte(digest), 0o644))
	fmt.Printf("PASS: native %s/%s binary, local help, literal argv, discovery, modes, trace/timing, exit codes, and real kubectl dispatch\n%s", runtime.GOOS, runtime.GOARCH, digest)
}
