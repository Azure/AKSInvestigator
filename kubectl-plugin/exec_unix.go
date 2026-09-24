//go:build !windows

package main

import (
	"fmt"
	"os"
	"syscall"
)

func execute(kubectl string, args []string) int {
	// Replace this process: exit codes, terminal handles, and signals go straight
	// to kubectl (including process-group cancellation by the benchmark runner).
	if err := syscall.Exec(kubectl, append([]string{kubectl}, args...), os.Environ()); err != nil {
		fmt.Fprintln(os.Stderr, "kubectl-investigate: could not start kubectl; check its installation and permissions.")
		return 1
	}
	return 0
}
