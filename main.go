package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync"
	"time"
)

// Config
const (
	INPUT_SOCK  = "/tmp/hyprkeys.sock" // UDP socket from Hyprland
	PUB_SOCK    = "/tmp/hkd.sock"      // Stream socket for Wrappers/Overlays
	BUFFER_SIZE = 1024
)

// Packet from C++ (8 bytes)
type RawPacket struct {
	KeyCode int32
	State   int32 // 0=Up, 1=Down
}

// Clean Event for Clients
type PubEvent struct {
	Key   int32   `json:"key"`
	State string  `json:"state"` // "DOWN" or "UP"
	KPS   int     `json:"kps"`
	Total uint64  `json:"total"`
}

// State Tracking
type KeyStats struct {
	Total   uint64
	History []int64 // Timestamps in ms
}

var (
	stateMux sync.Mutex
	keyState = make(map[int32]*KeyStats)
	clients  = make(map[net.Conn]bool)
	clientMux sync.Mutex
)

func main() {
	// 1. Cleanup old sockets
	os.Remove(INPUT_SOCK)
	os.Remove(PUB_SOCK)

	// 2. Setup UDP Input (The Sink)
	addr, _ := net.ResolveUnixAddr("unixgram", INPUT_SOCK)
	conn, err := net.ListenUnixgram("unixgram", addr)
	if err != nil {
		panic(fmt.Sprintf("Failed to bind UDP: %v", err))
	}
	// Important: Set permissions so Hyprland (user) can write to it
	os.Chmod(INPUT_SOCK, 0777)
	fmt.Printf(" [hkd] Listening for raw crap on %s\n", INPUT_SOCK)

	// 3. Setup Pub Socket (The Broadcast)
	pubListener, err := net.Listen("unix", PUB_SOCK)
	if err != nil {
		panic(err)
	}
	os.Chmod(PUB_SOCK, 0777)

	// 4. Handle Publishers
	go acceptClients(pubListener)

	// 5. Prune KPS Loop (Runs every 100ms)
	// This ensures KPS drops to 0 if you stop typing
	go func() {
		for {
			time.Sleep(100 * time.Millisecond)
			now := time.Now().UnixMilli()

			stateMux.Lock()
			for _, stats := range keyState {
				// Prune timestamps older than 1s
				valid := stats.History[:0]
				for _, ts := range stats.History {
					if now-ts <= 1000 {
						valid = append(valid, ts)
					}
				}
				stats.History = valid
			}
			stateMux.Unlock()
		}
	}()

	// 6. Main Loop: Read Raw Data
	buf := make([]byte, 8)
	for {
		n, _, err := conn.ReadFrom(buf)
		if err != nil || n != 8 {
			continue
		}

		// Parse C++ binary struct
		var code, state int32
		r := bytes.NewReader(buf)
		binary.Read(r, binary.LittleEndian, &code)
		binary.Read(r, binary.LittleEndian, &state)

		// Update Math
		stateMux.Lock()
		if _, exists := keyState[code]; !exists {
			keyState[code] = &KeyStats{History: make([]int64, 0)}
		}
		stats := keyState[code]

		strState := "UP"
		if state == 1 {
			strState = "DOWN"
			stats.Total++
			stats.History = append(stats.History, time.Now().UnixMilli())
		}

		// Instant KPS calculation
		currentKPS := len(stats.History)
		currentTotal := stats.Total
		stateMux.Unlock()

		// Broadcast
		broadcast(PubEvent{
			Key:   code,
			State: strState,
			KPS:   currentKPS,
			Total: currentTotal,
		})
	}
}

func acceptClients(l net.Listener) {
	for {
		fd, err := l.Accept()
		if err != nil { return }

		clientMux.Lock()
		clients[fd] = true
		clientMux.Unlock()

		fmt.Println(" [hkd] New client connected.")
	}
}

func broadcast(e PubEvent) {
	data, _ := json.Marshal(e)
	data = append(data, '\n') // NDJSON

	clientMux.Lock()
	defer clientMux.Unlock()

	for client := range clients {
		// Set write deadline to prevent blocking if a client hangs
		client.SetWriteDeadline(time.Now().Add(10 * time.Millisecond))
		_, err := client.Write(data)
		if err != nil {
			client.Close()
			delete(clients, client)
		}
	}
}
