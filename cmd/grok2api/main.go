package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hm2899/grokcli-2api/internal/admin"
	"github.com/hm2899/grokcli-2api/internal/auth"
	"github.com/hm2899/grokcli-2api/internal/buildinfo"
	"github.com/hm2899/grokcli-2api/internal/config"
	"github.com/hm2899/grokcli-2api/internal/models"
	appruntime "github.com/hm2899/grokcli-2api/internal/runtime"
	"github.com/hm2899/grokcli-2api/internal/server"
	"github.com/hm2899/grokcli-2api/internal/store/postgres"
	"github.com/hm2899/grokcli-2api/internal/store/redis"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		slog.Error("invalid configuration", "error", err)
		os.Exit(2)
	}

	stop, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	readiness := appruntime.StartReadinessProbe(stop, cfg, "migrations")
	var store *postgres.Connector
	var adminSessions admin.SessionVerifier
	var redisClient *redis.Client
	if cfg.GoAdminRead || cfg.GoAdminWrite || cfg.GoChat || cfg.GoMessages || cfg.GoResponses {
		redisClient = redis.New(cfg.RedisURL, cfg.RedisPrefix)
	}
	if cfg.GoAdminRead || cfg.GoAdminWrite {
		adminSessions = redisClient
	}
	if cfg.GoPublicRead || cfg.GoAdminRead || cfg.GoAdminWrite || cfg.GoChat || cfg.GoMessages || cfg.GoResponses {
		ctx, done := context.WithTimeout(context.Background(), 10*time.Second)
		opened, err := postgres.Open(ctx, cfg.DatabaseURL)
		done()
		if err != nil {
			slog.Warn("read-only store unavailable; staged routes remain fail-closed", "error", err)
		} else {
			store = opened
			defer store.Close()
			if adminSessions == nil {
				adminSessions = store
			}
		}
	}
	handler := server.NewMux(server.Options{
		Ready:             readiness.Ready,
		Reason:            readiness.Reason,
		StaticDir:         cfg.StaticDir,
		PublicReadEnabled: cfg.GoPublicRead,
		AdminReadEnabled:  cfg.GoAdminRead,
		AdminWriteEnabled: cfg.GoAdminWrite,
		ChatEnabled:       cfg.GoChat,
		MessagesEnabled:   cfg.GoMessages,
		ResponsesEnabled:  cfg.GoResponses,
		APIKeys:           auth.NewAPIKeyVerifier(cfg, store),
		Models:            models.NewCatalog(cfg, store),
		Store:             store,
		AdminSessions:     adminSessions,
		PickObserver:      redis.NewPickObserver(redisClient),
		AffinityStore:     redis.NewChatAffinity(redisClient, cfg.SSEKeepalive*1800),
		Config:            cfg,
		RegistrationURL:   cfg.RegistrationServiceURL,
		RegistrationToken: cfg.RegistrationToken,
	})
	httpServer := &http.Server{
		Addr:              cfg.Address(),
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       30 * time.Second,
	}

	go func() {
		<-stop.Done()
		ctx, done := context.WithTimeout(context.Background(), 30*time.Second)
		defer done()
		if err := httpServer.Shutdown(ctx); err != nil {
			slog.Error("graceful shutdown failed", "error", err)
		}
	}()

	slog.Info("starting migration-safe Go probe server",
		"address", cfg.Address(),
		"implementation", buildinfo.Implementation,
		"version", buildinfo.Version,
	)
	if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server stopped", "error", err)
		os.Exit(1)
	}
}
