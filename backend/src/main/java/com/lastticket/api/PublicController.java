package com.lastticket.api;

import com.lastticket.config.Tokens;
import com.lastticket.inventory.InventoryQueries;
import com.lastticket.inventory.InventoryQueries.Availability;
import com.lastticket.inventory.InventoryQueries.EventView;
import com.lastticket.inventory.InventoryService;
import com.lastticket.inventory.Reservation;
import com.lastticket.waitingroom.WaitingRoom;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.security.SecurityRequirement;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.http.CacheControl;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
@Tag(name = "Buying tickets")
public class PublicController {

    public record Session(String token, String userId, Instant expiresAt) {}

    public record ReserveRequest(@NotNull UUID inventoryId, @NotNull @Min(1) @Max(10) Integer quantity) {}

    private final Tokens tokens;
    private final InventoryQueries queries;
    private final InventoryService inventory;
    private final WaitingRoom waitingRoom;

    public PublicController(Tokens tokens, InventoryQueries queries, InventoryService inventory, WaitingRoom waitingRoom) {
        this.tokens = tokens;
        this.queries = queries;
        this.inventory = inventory;
        this.waitingRoom = waitingRoom;
    }

    @PostMapping("/sessions")
    @ResponseStatus(HttpStatus.CREATED)
    @Operation(summary = "Start an anonymous session", description = "Returns a bearer token identifying a new shopper.")
    Session createSession() {
        String userId = UUID.randomUUID().toString();
        Instant now = Instant.now();
        return new Session(tokens.session(userId, now), userId, now.plus(java.time.Duration.ofHours(6)));
    }

    @GetMapping("/events")
    @Operation(summary = "List on-sales, soonest first")
    Page<EventView> events(@RequestParam(required = false) String cursor, @RequestParam(defaultValue = "20") @Min(1) @Max(100) int limit) {
        String[] c = Page.decode(cursor, 2);
        List<EventView> rows;
        try {
            rows = c == null ? queries.events(null, null, limit + 1) : queries.events(Instant.parse(c[0]), UUID.fromString(c[1]), limit + 1);
        } catch (java.time.format.DateTimeParseException | IllegalArgumentException e) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_CURSOR", "cursor is not one this API issued.");
        }
        return Page.of(rows, limit, e -> e.onSaleAt() + "|" + e.id());
    }

    @GetMapping("/events/{eventId}")
    @Operation(summary = "One on-sale, with the server clock")
    EventView event(@PathVariable UUID eventId) {
        return queries.event(eventId);
    }

    @GetMapping("/events/{eventId}/availability")
    @Operation(summary = "Available-to-promise per section", description = "Never blocks on writers. May be up to 1 s old; asOf says how old.")
    ResponseEntity<Availability> availability(@PathVariable UUID eventId) {
        return ResponseEntity.ok().cacheControl(CacheControl.maxAge(java.time.Duration.ofSeconds(1)).cachePublic()).body(queries.availability(eventId));
    }

    @PostMapping("/events/{eventId}/queue")
    @SecurityRequirement(name = "session")
    @Operation(summary = "Join the waiting room", description = "Idempotent: joining again returns the place you already have.")
    WaitingRoom.Status join(@PathVariable UUID eventId, Authentication auth) {
        return waitingRoom.join(eventId, auth.getName());
    }

    @GetMapping("/events/{eventId}/queue")
    @SecurityRequirement(name = "session")
    @Operation(summary = "Your place in the waiting room", description = "Poll no faster than pollAfterMs. When ADMITTED, carries the admission token.")
    WaitingRoom.Status queueStatus(@PathVariable UUID eventId, Authentication auth) {
        return waitingRoom.status(eventId, auth.getName());
    }

    @PostMapping("/reservations")
    @ResponseStatus(HttpStatus.CREATED)
    @SecurityRequirement(name = "session")
    @Operation(summary = "Hold tickets", description = """
            Places a hold with a hard expiry. Requires an admission token from the waiting room. \
            Safe to retry with the same Idempotency-Key: you get the same reservation back, never a second one. \
            409 SOLD_OUT / ALREADY_HOLDING / LIMIT_EXCEEDED / NOT_ON_SALE; 503 CONTENTION means nothing happened, retry.""")
    Reservation reserve(@Valid @RequestBody ReserveRequest body,
                        @RequestHeader("Idempotency-Key") @NotBlank @Pattern(regexp = "[A-Za-z0-9._-]{8,100}") String idempotencyKey,
                        @RequestHeader("X-Admission-Token") @NotBlank String admissionToken,
                        Authentication auth) {
        tokens.requireAdmission(admissionToken, auth.getName(), inventory.eventOf(body.inventoryId()));
        return inventory.reserve(auth.getName(), body.inventoryId(), body.quantity(), idempotencyKey);
    }

    @GetMapping("/reservations")
    @SecurityRequirement(name = "session")
    @Operation(summary = "Your reservations, newest first")
    Page<Reservation> reservations(@RequestParam(required = false) String cursor, @RequestParam(defaultValue = "20") @Min(1) @Max(100) int limit,
                                   Authentication auth) {
        String[] c = Page.decode(cursor, 2);
        List<Reservation> rows;
        try {
            rows = c == null ? inventory.list(auth.getName(), null, null, limit + 1)
                    : inventory.list(auth.getName(), Instant.parse(c[0]), UUID.fromString(c[1]), limit + 1);
        } catch (java.time.format.DateTimeParseException | IllegalArgumentException e) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_CURSOR", "cursor is not one this API issued.");
        }
        return Page.of(rows, limit, r -> r.createdAt() + "|" + r.id());
    }

    @GetMapping("/reservations/{id}")
    @SecurityRequirement(name = "session")
    @Operation(summary = "One of your reservations")
    Reservation reservation(@PathVariable UUID id, Authentication auth) {
        return inventory.find(auth.getName(), id);
    }

    @PostMapping("/reservations/{id}/confirm")
    @SecurityRequirement(name = "session")
    @Operation(summary = "Complete the purchase", description = "Idempotent. 410 HOLD_EXPIRED once the hold's deadline has passed.")
    Reservation confirm(@PathVariable UUID id, Authentication auth) {
        return inventory.confirm(auth.getName(), id);
    }

    @DeleteMapping("/reservations/{id}")
    @SecurityRequirement(name = "session")
    @Operation(summary = "Give the tickets back", description = "Idempotent. 409 ALREADY_CLOSED if the order was already confirmed.")
    Reservation release(@PathVariable UUID id, Authentication auth) {
        return inventory.release(auth.getName(), id);
    }
}
