package com.lastticket.inventory;

import com.lastticket.api.ApiException;
import java.util.UUID;
import org.springframework.http.HttpStatus;

/**
 * The inventory aggregate for one (event, ticket type, section). Pure: every command either returns the
 * {@link Change} it would cause or throws; nothing here touches I/O. Persistence appends the change at
 * {@code version + 1} and fails if someone else got there first.
 */
public record Inventory(UUID id, UUID eventId, String ticketType, String section, int priceCents,
                        long version, int total, int held, int sold) {

    public enum EventType { STOCK_ADDED, STOCK_CORRECTED, HOLD_PLACED, HOLD_CONFIRMED, HOLD_EXPIRED, HOLD_RELEASED }

    /** One event's effect on the aggregate. */
    public record Change(EventType type, int totalDelta, int heldDelta, int soldDelta) {}

    public int available() {
        return total - held - sold;
    }

    public Change placeHold(int qty) {
        requirePositive(qty);
        if (qty > available()) {
            throw new ApiException(HttpStatus.CONFLICT, "SOLD_OUT",
                    available() == 0 ? "No tickets left in this section." : "Only " + available() + " left in this section.");
        }
        return new Change(EventType.HOLD_PLACED, 0, qty, 0);
    }

    public Change confirmHold(int qty) {
        requireHeld(qty);
        return new Change(EventType.HOLD_CONFIRMED, 0, -qty, qty);
    }

    public Change expireHold(int qty) {
        requireHeld(qty);
        return new Change(EventType.HOLD_EXPIRED, 0, -qty, 0);
    }

    public Change releaseHold(int qty) {
        requireHeld(qty);
        return new Change(EventType.HOLD_RELEASED, 0, -qty, 0);
    }

    /**
     * Stock release (delta &gt; 0) or correction (delta &lt; 0). A correction may only remove tickets nobody holds or
     * owns: outstanding holds are promises and are never broken by a stock change.
     */
    public Change adjustStock(int delta) {
        if (delta == 0) {
            throw new ApiException(HttpStatus.UNPROCESSABLE_ENTITY, "INVALID_DELTA", "delta must not be 0.");
        }
        if (delta > 0) {
            return new Change(EventType.STOCK_ADDED, delta, 0, 0);
        }
        if (-delta > available()) {
            throw new ApiException(HttpStatus.CONFLICT, "CORRECTION_BELOW_COMMITTED",
                    "Cannot remove " + -delta + ": " + (held + sold) + " of " + total
                            + " are held or sold. At most " + available() + " can be removed right now.");
        }
        return new Change(EventType.STOCK_CORRECTED, delta, 0, 0);
    }

    public Inventory apply(Change c) {
        return new Inventory(id, eventId, ticketType, section, priceCents, version + 1,
                total + c.totalDelta(), held + c.heldDelta(), sold + c.soldDelta());
    }

    private static void requirePositive(int qty) {
        if (qty <= 0) {
            throw new ApiException(HttpStatus.UNPROCESSABLE_ENTITY, "INVALID_QUANTITY", "quantity must be positive.");
        }
    }

    private void requireHeld(int qty) {
        requirePositive(qty);
        if (qty > held) {
            // Would mean the snapshot and the reservation table disagree. Fail loudly rather than go negative.
            throw new IllegalStateException("inventory " + id + " holds " + held + " but asked to close " + qty);
        }
    }
}
