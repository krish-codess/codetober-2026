// shipd is the migration controller.
//
//	shipd serve              run the API and drive migrations
//	shipd init               one-time setup as a superuser: roles, grants, pgroll state
//	shipd apply FILE [--complete]   submit a migration file and wait for it
//	shipd plan FILE [--full] diff a desired-schema file against the live schema
//	shipd openapi            print the OpenAPI document
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"slices"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/krish-codess/codetober-2026/SHIP/internal/api"
	"github.com/krish-codess/codetober-2026/SHIP/internal/engine"
	"github.com/krish-codess/codetober-2026/SHIP/internal/plan"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func must(key string) string {
	v := os.Getenv(key)
	if v == "" {
		fmt.Fprintf(os.Stderr, "%s is required; see .env.example\n", key)
		os.Exit(2)
	}
	return v
}

func main() {
	level := slog.LevelInfo
	_ = level.UnmarshalText([]byte(env("LOG_LEVEL", "info")))
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})).With("service", "shipd")
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cmd := "serve"
	if len(os.Args) > 1 {
		cmd = os.Args[1]
	}
	var err error
	switch cmd {
	case "serve":
		err = serve(ctx, log)
	case "init":
		err = engine.Init(ctx, must("ADMIN_DATABASE_URL"), must("DATABASE_URL"), must("APP_DATABASE_URL"))
	case "apply":
		err = apply(ctx, log, os.Args[2:])
	case "plan":
		err = planCmd(ctx, os.Args[2:])
	case "openapi":
		_, a := api.New(nil, api.Config{}, log)
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		err = enc.Encode(a.OpenAPI())
	default:
		err = fmt.Errorf("unknown command %q", cmd)
	}
	if err != nil {
		log.Error(cmd+" failed", "err", err)
		os.Exit(1)
	}
}

func serve(ctx context.Context, log *slog.Logger) error {
	cfg := api.Config{OperatorToken: must("SHIPD_OPERATOR_TOKEN"), ViewerToken: must("SHIPD_VIEWER_TOKEN")}
	eng, err := engine.New(ctx, must("DATABASE_URL"), env("APP_DB_ROLE", "shop_app"), log)
	if err != nil {
		return err
	}
	handler, _ := api.New(eng, cfg, log)
	srv := &http.Server{Addr: env("SHIPD_ADDR", ":8080"), Handler: handler,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Minute /* verify scans the whole table */, IdleTimeout: 60 * time.Second}
	var lost atomic.Bool
	go func() {
		select {
		case <-ctx.Done():
		case <-eng.Lost():
			lost.Store(true)
		}
		shut, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shut)
	}()
	log.Info("listening", "addr", srv.Addr)
	err = srv.ListenAndServe()
	eng.Close() // a migration mid-expand is left as is; the next start rolls it back
	if lost.Load() {
		return errors.New("lost the leader lock")
	}
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func readJSON(path string, v any) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, v)
}

func apply(ctx context.Context, log *slog.Logger, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: shipd apply FILE [--complete]")
	}
	var in engine.Submit
	if err := readJSON(args[0], &in); err != nil {
		return err
	}
	// Already applied? Then there is nothing to do, and no reason to wait for the running controller's leader lock.
	if db, err := sql.Open("postgres", must("DATABASE_URL")); err == nil {
		var done bool
		_ = db.QueryRowContext(ctx, `SELECT true FROM shipd.runs WHERE name = $1 AND state = 'completed'`, in.Name).Scan(&done)
		db.Close()
		if done {
			log.Warn("already applied", "migration", in.Name)
			return nil
		}
	}
	startCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	eng, err := engine.New(startCtx, must("DATABASE_URL"), env("APP_DB_ROLE", "shop_app"), log)
	if err != nil {
		return fmt.Errorf("%w (if a controller is running, submit through its API instead)", err)
	}
	defer eng.Close()
	run, _, err := eng.Submit(ctx, in, "cli-"+in.Name)
	if err != nil {
		return describe(err)
	}
	if run, err = eng.Wait(ctx, run.ID, engine.Expanded); err != nil {
		return err
	}
	if run.State == engine.Expanded && slices.Contains(args, "--complete") {
		if _, err := eng.Complete(ctx, run.ID); err != nil {
			return describe(err)
		}
		if run, err = eng.Wait(ctx, run.ID, engine.Expanded); err != nil {
			return err
		}
	}
	_ = json.NewEncoder(os.Stdout).Encode(run)
	if run.State == engine.Reverted || run.State == engine.Failed || run.Reason != "" {
		return fmt.Errorf("migration %s is %s: %s", run.Name, run.State, run.Reason)
	}
	return nil
}

func describe(err error) error {
	var e *engine.Error
	if errors.As(err, &e) && len(e.Details) > 0 {
		return fmt.Errorf("%s: %v", e.Msg, e.Details)
	}
	return err
}

func planCmd(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: shipd plan FILE [--full]")
	}
	var desired plan.Desired
	if err := readJSON(args[0], &desired); err != nil {
		return err
	}
	db, err := sql.Open("postgres", must("DATABASE_URL"))
	if err != nil {
		return err
	}
	defer db.Close()
	current, err := plan.Load(ctx, db)
	if err != nil {
		return err
	}
	p, err := plan.Diff(current, desired, 0)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	if slices.Contains(args, "--full") {
		return enc.Encode(p)
	}
	// Exactly the body POST /v1/migrations takes: commit it under migrations/.
	return enc.Encode(engine.Submit{Name: p.Name, CompatibleAppVersions: p.CompatibleAppVersions, Operations: p.Operations})
}
