package main

import (
	"fmt"
	"log"
	"net/http"
	"runtime"
	"strconv"
	"sync"
)

const (
	defaultAddr = ":8000"
	maxMiB      = 1024 // Safety guard: do not accidentally request several GiB on the host.
	pageSize    = 4096
)

var (
	// Keep allocated memory reachable so the garbage collector cannot reclaim it.
	memoryMu sync.Mutex
	memory   [][]byte
	burnSink uint64
)

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/eat", eatHandler)
	mux.HandleFunc("/burn", burnHandler)

	addr := defaultAddr
	log.Printf("api listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

func healthHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = fmt.Fprintln(w, "ok")
}

func eatHandler(w http.ResponseWriter, r *http.Request) {
	mb, err := strconv.Atoi(r.URL.Query().Get("mb"))
	if err != nil || mb <= 0 || mb > maxMiB {
		http.Error(w, fmt.Sprintf("mb must be an integer from 1 to %d", maxMiB), http.StatusBadRequest)
		return
	}

	buf := make([]byte, mb*1024*1024)
	// Touch one byte per page so the OS has to back the allocation with memory.
	for i := 0; i < len(buf); i += pageSize {
		buf[i] = byte(i / pageSize)
	}
	buf[len(buf)-1] = 1

	memoryMu.Lock()
	memory = append(memory, buf)
	totalMiB := 0
	for _, block := range memory {
		totalMiB += len(block) / (1024 * 1024)
	}
	memoryMu.Unlock()

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = fmt.Fprintf(w, "allocated=%d MiB held=%d MiB\n", mb, totalMiB)
}

func burnHandler(w http.ResponseWriter, _ *http.Request) {
	// Intentionally never returns: this handler keeps one request goroutine busy.
	// Keep a changing value observable to make the CPU work explicit.
	var n uint64
	for {
		n = n*1664525 + 1013904223
		if n&0xFFFF == 0 {
			burnSink = n
			runtime.KeepAlive(burnSink)
		}
	}
}
