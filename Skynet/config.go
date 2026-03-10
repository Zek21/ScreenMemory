package main

import (
	"os"
	"strconv"
	"strings"
)

// Config holds runtime settings loaded from environment variables.
type Config struct {
	Port       int
	Workers    []string
	RingSize   int
	MaxRetries int
}

func LoadConfig() Config {
	cfg := Config{
		Port:       8420,
		Workers:    []string{"alpha", "beta", "gamma", "delta", "orchestrator"},
		RingSize:   100,
		MaxRetries: 3,
	}

	if v := os.Getenv("SKYNET_PORT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.Port = n
		}
	}
	if v := os.Getenv("SKYNET_WORKERS"); v != "" {
		names := strings.Split(v, ",")
		clean := make([]string, 0, len(names))
		for _, name := range names {
			if t := strings.TrimSpace(name); t != "" {
				clean = append(clean, t)
			}
		}
		if len(clean) > 0 {
			cfg.Workers = clean
		}
	}
	if v := os.Getenv("SKYNET_RING_SIZE"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.RingSize = n
		}
	}
	if v := os.Getenv("SKYNET_MAX_RETRIES"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			cfg.MaxRetries = n
		}
	}

	return cfg
}
