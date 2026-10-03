package com.lastticket.api;

import com.lastticket.intake.AttemptIntake;
import com.lastticket.inventory.Inventory;
import com.lastticket.inventory.InventoryQueries;
import com.lastticket.inventory.InventoryService;
import com.lastticket.messaging.DeadLetters;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.security.SecurityRequirement;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/admin")
@Tag(name = "Operations")
@SecurityRequirement(name = "adminKey")
public class AdminController {
    private static final int MAX_BATCH_BYTES = 5 * 1024 * 1024;

    public record InventorySpec(@NotBlank @Pattern(regexp = "[A-Z0-9_]{1,32}") String ticketType,
                                @NotBlank @Pattern(regexp = "[A-Z0-9_]{1,32}") String section,
                                @NotNull @Min(0) Integer priceCents, @NotNull @Min(1) @Max(1_000_000) Integer total) {}

    public record EventSpec(@NotBlank @Size(max = 200) String name, @NotNull Instant onSaleAt,
                            @NotNull @Min(5) @Max(3600) Integer holdSeconds, @NotNull @Min(1) @Max(10) Integer maxPerUser,
                            @NotNull @Min(1) @Max(10000) Integer admissionRatePerSec,
                            @NotEmpty @Size(max = 500) List<@Valid InventorySpec> inventory) {}

    public record Adjustment(@NotNull @Min(-1_000_000) @Max(1_000_000) Integer delta,
                             @NotNull @Min(0) Long expectedVersion, @NotBlank @Size(max = 500) String reason) {}

    private final InventoryService inventory;
    private final InventoryQueries queries;
    private final DeadLetters deadLetters;
    private final AttemptIntake intake;

    public AdminController(InventoryService inventory, InventoryQueries queries, DeadLetters deadLetters, AttemptIntake intake) {
        this.inventory = inventory;
        this.queries = queries;
        this.deadLetters = deadLetters;
        this.intake = intake;
    }

    @PutMapping("/events/{eventId}")
    @Operation(summary = "Create an on-sale with its inventory", description = "Idempotent on the id: 201 when created, 200 when it already existed (body ignored).")
    ResponseEntity<InventoryQueries.EventView> createEvent(@PathVariable UUID eventId, @Valid @RequestBody EventSpec spec) {
        boolean created = inventory.createEvent(new InventoryService.NewEvent(eventId, spec.name(), spec.onSaleAt(), spec.holdSeconds(),
                spec.maxPerUser(), spec.admissionRatePerSec(), spec.inventory().stream()
                .map(i -> new InventoryService.NewInventory(i.ticketType(), i.section(), i.priceCents(), i.total())).toList()));
        return ResponseEntity.status(created ? HttpStatus.CREATED : HttpStatus.OK).body(queries.event(eventId));
    }

    @PostMapping("/inventory/{inventoryId}/adjustments")
    @Operation(summary = "Release more stock (delta > 0) or correct it down (delta < 0)", description = """
            Appended to the aggregate's event stream like any other change. Never breaks an outstanding hold: a \
            correction below held + sold is refused with 409 CORRECTION_BELOW_COMMITTED. \
            expectedVersion makes it safe to retry: a repeat fails with 409 VERSION_MISMATCH instead of applying twice.""")
    Inventory adjust(@PathVariable UUID inventoryId, @Valid @RequestBody Adjustment body) {
        return inventory.adjustStock(inventoryId, body.delta(), body.expectedVersion(), body.reason());
    }

    @GetMapping("/inventory/{inventoryId}/events")
    @Operation(summary = "One section's event stream, oldest first")
    Page<InventoryQueries.StreamEvent> stream(@PathVariable UUID inventoryId, @RequestParam(required = false) String cursor,
                                              @RequestParam(defaultValue = "100") @Min(1) @Max(1000) int limit) {
        return Page.of(queries.stream(inventoryId, seq(cursor), limit + 1), limit, e -> Long.toString(e.seq()));
    }

    @GetMapping("/events/{eventId}/position")
    @Operation(summary = "Inventory position as of an instant", description = "Rebuilt from the event stream alone: every event with occurred_at <= at.")
    List<InventoryQueries.Position> position(@PathVariable UUID eventId, @RequestParam Instant at) {
        return queries.positionAsOf(eventId, at);
    }

    @GetMapping("/events/{eventId}/analytics")
    @Operation(summary = "Oversell-attempt, conversion and expiry rates", description = "Built by the Kafka consumer, so it trails the sale by the outbox interval.")
    InventoryQueries.Analytics analytics(@PathVariable UUID eventId) {
        return queries.analytics(eventId);
    }

    @GetMapping("/dead-letters")
    @Operation(summary = "Messages a consumer gave up on, oldest first")
    Page<DeadLetters.DeadLetter> deadLetters(@RequestParam(required = false) String cursor, @RequestParam(defaultValue = "50") @Min(1) @Max(500) int limit) {
        return Page.of(deadLetters.list(seq(cursor), limit + 1), limit, d -> Long.toString(d.id()));
    }

    @PostMapping("/dead-letters/{id}/replay")
    @Operation(summary = "Send a dead letter back to its topic", description = "Safe to repeat: consumers deduplicate on the message id.")
    DeadLetters.DeadLetter replay(@PathVariable long id) {
        return deadLetters.replay(id);
    }

    @PostMapping(value = "/attempts/ingest", consumes = {"application/x-ndjson", "text/plain"})
    @Operation(summary = "Ingest a batch of purchase attempts (NDJSON, one attempt per line)", description = """
            Every line is stored verbatim, then validated; bad lines are quarantined with a reason. \
            Re-sending the same batchId is safe: lines that already have an outcome are skipped.""")
    AttemptIntake.Summary ingest(@RequestParam UUID batchId, @RequestBody String body) {
        if (body.length() > MAX_BATCH_BYTES) {
            throw new ApiException(HttpStatus.PAYLOAD_TOO_LARGE, "BATCH_TOO_LARGE", "Split the batch: at most 5 MB per request.");
        }
        return intake.ingest(batchId, body);
    }

    @GetMapping("/attempts/quarantine")
    @Operation(summary = "Quarantined attempt lines with their original payload, oldest first")
    Page<AttemptIntake.Quarantined> quarantine(@RequestParam(required = false) String cursor, @RequestParam(defaultValue = "50") @Min(1) @Max(500) int limit) {
        return Page.of(intake.quarantine(seq(cursor), limit + 1), limit, q -> Long.toString(q.id()));
    }

    private static long seq(String cursor) {
        String[] c = Page.decode(cursor, 1);
        try {
            return c == null ? 0 : Long.parseLong(c[0]);
        } catch (NumberFormatException e) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_CURSOR", "cursor is not one this API issued.");
        }
    }
}
