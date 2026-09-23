package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
)

func execute(kubectl string, args []string) int {
	// Windows broadcasts Ctrl+C to processes sharing the console. Let kubectl
	// clean up the remote exec before the parent exits. Do not use signal.Ignore,
	// whose console setting would be inherited by the child.
	interrupt := make(chan os.Signal, 1)
	signal.Notify(interrupt, os.Interrupt)
	defer signal.Stop(interrupt)
	// Go handles CreateProcess quoting; never join argv into local shell code.
	child := exec.Command(kubectl, args...)
	child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := child.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return exit.ExitCode()
		}
		fmt.Fprintln(os.Stderr, "kubectl-investigate: could not start kubectl.exe; check its installation and permissions.")
		return 1
	}
	return 0
}
