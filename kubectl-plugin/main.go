// kubectl-investigate asks the in-cluster Holmes CLI to investigate a question.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"

	"golang.org/x/term"
)

const usage = `kubectl-investigate: ask AKS Investigator to investigate a cluster question.

Usage:
  kubectl investigate "<question>" [holmes_cli flags...]
	kubectl investigate --help

Options:
	-h, --help  Show this help and exit (before the question; no cluster access).

Flags after the question are forwarded to Holmes unchanged. Common examples:
	--model saturn-vllm  Select the in-cluster Saturn model.
	-i, --interactive   Keep the conversational prompt open.
	--no-interactive    Answer once and exit (default).

Examples:
  kubectl investigate "how many pods are in my cluster"
  kubectl investigate "why is pod X crashlooping" --model saturn-vllm

Environment:
  INVESTIGATOR_NAMESPACE  namespace containing AKS Investigator (default: aks-investigator)
  INVESTIGATOR_RELEASE    Helm release (default: aks-investigator)
  INVESTIGATOR_DEPLOY     deployment name; overrides release and discovery
  INVESTIGATOR_SELECTOR   fallback deployment selector (default: app=holmes)
  INVESTIGATOR_HOME       HOME inside the pod (default: /tmp)
`

func valueOrDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func usesBraintrust(args []string) bool {
	previous := ""
	for _, arg := range args {
		if arg == "--" {
			break
		}
		if arg == "--trace=braintrust" || (previous == "--trace" && arg == "braintrust") {
			return true
		}
		previous = arg
	}
	return false
}

func discover(kubectl, namespace, release, selector string) (string, error) {
	deployment := release + "-holmes"
	if err := exec.Command(kubectl, "get", "deployment", "-n", namespace, deployment).Run(); err == nil {
		return deployment, nil
	}
	out, err := exec.Command(kubectl, "get", "deployments", "-n", namespace, "-l", selector, "-o", "json").Output()
	if err != nil {
		return "", fmt.Errorf("cannot discover Holmes deployments in namespace %q; check kubectl context, authentication, and permissions", namespace)
	}
	var deployments struct {
		Items []struct {
			Metadata struct {
				Name string `json:"name"`
			} `json:"metadata"`
		} `json:"items"`
	}
	if err := json.Unmarshal(out, &deployments); err != nil {
		return "", errors.New("kubectl returned invalid deployment data")
	}
	switch len(deployments.Items) {
	case 0:
		return "", fmt.Errorf("no Holmes deployment found in namespace %q (tried %q and selector %q); set INVESTIGATOR_NAMESPACE or INVESTIGATOR_DEPLOY", namespace, deployment, selector)
	case 1:
		if deployments.Items[0].Metadata.Name == "" {
			return "", errors.New("kubectl returned a deployment without a name")
		}
		return deployments.Items[0].Metadata.Name, nil
	default:
		return "", fmt.Errorf("multiple Holmes deployments match %q in namespace %q; set INVESTIGATOR_RELEASE or INVESTIGATOR_DEPLOY", selector, namespace)
	}
}

func run(args []string) int {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return 64
	}
	// Only a leading help flag belongs to the plugin. Do not consume Holmes
	// flags or text containing "--help" after the question.
	if args[0] == "--help" || args[0] == "-h" {
		fmt.Fprint(os.Stdout, usage)
		return 0
	}
	traced := usesBraintrust(args)
	if traced && (os.Getenv("BRAINTRUST_API_KEY") == "" || os.Getenv("BRAINTRUST_ORG") == "") {
		fmt.Fprintln(os.Stderr, "kubectl-investigate: --trace braintrust requires local BRAINTRUST_API_KEY and BRAINTRUST_ORG.")
		return 64
	}
	kubectlName := "kubectl"
	if runtime.GOOS == "windows" {
		// Require a native executable, not a .cmd/.ps1 interpreter wrapper.
		kubectlName += ".exe"
	}
	kubectl, err := exec.LookPath(kubectlName)
	if err != nil {
		fmt.Fprintln(os.Stderr, "kubectl-investigate:", kubectlName, "was not found on PATH; install kubectl and add its directory to PATH.")
		return 127
	}
	namespace := valueOrDefault("INVESTIGATOR_NAMESPACE", "aks-investigator")
	deployment := os.Getenv("INVESTIGATOR_DEPLOY")
	if deployment == "" {
		deployment, err = discover(kubectl, namespace,
			valueOrDefault("INVESTIGATOR_RELEASE", "aks-investigator"),
			valueOrDefault("INVESTIGATOR_SELECTOR", "app=holmes"))
		if err != nil {
			fmt.Fprintln(os.Stderr, "kubectl-investigate:", err)
			return 1
		}
	}
	remote := []string{"env", "HOME=" + valueOrDefault("INVESTIGATOR_HOME", "/tmp")}
	if traced {
		// Credentials are visible in process arguments and potentially audit logs.
		remote = append(remote, "BRAINTRUST_API_KEY="+os.Getenv("BRAINTRUST_API_KEY"),
			"BRAINTRUST_ORG="+os.Getenv("BRAINTRUST_ORG"))
	}
	remote = append(remote, "python", "holmes_cli.py", "ask")
	remote = append(remote, args...)
	explicitMode := false
	for _, arg := range args {
		if arg == "-i" || arg == "--interactive" || arg == "-n" || arg == "--no-interactive" {
			explicitMode = true
			break
		}
	}
	if !explicitMode {
		remote = append(remote, "--no-interactive")
	}
	command := []string{"exec"}
	if term.IsTerminal(int(os.Stdin.Fd())) && term.IsTerminal(int(os.Stdout.Fd())) {
		command = append(command, "-it")
	}
	command = append(command, "-n", namespace, "deploy/"+deployment, "--")
	if os.Getenv("INVESTIGATOR_TIMING") == "1" {
		fmt.Fprintln(os.Stderr, "__AKSI_TIMING_EXEC_REQUEST__")
		// Fixed remote shell code only; user input stays in positional arguments.
		command = append(command, "bash", "-c",
			`printf '__AKSI_TIMING_EXEC_STARTED__\n' >&2; exec "$@"`, "investigate-timing")
	}
	command = append(command, remote...)
	return execute(kubectl, command)
}

func main() {
	os.Exit(run(os.Args[1:]))
}
