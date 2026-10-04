package com.lastticket.api;

import io.swagger.v3.oas.annotations.media.Schema;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import java.util.function.Function;
import org.springframework.http.HttpStatus;

/** A keyset page. Pass {@code next} back as {@code cursor} to get the following page; absent means no more. */
public record Page<T>(List<T> items, @Schema(description = "Opaque. Absent on the last page.") String next) {

    /** Builds a page from {@code limit + 1} fetched rows: the extra row only signals that another page exists. */
    public static <T> Page<T> of(List<T> fetched, int limit, Function<T, String> cursorOf) {
        if (fetched.size() <= limit) {
            return new Page<>(fetched, null);
        }
        List<T> items = fetched.subList(0, limit);
        return new Page<>(items, encode(cursorOf.apply(items.get(limit - 1))));
    }

    static String encode(String raw) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(raw.getBytes(StandardCharsets.UTF_8));
    }

    /** @return the cursor's parts, or null when there is no cursor */
    public static String[] decode(String cursor, int parts) {
        if (cursor == null || cursor.isEmpty()) {
            return null;
        }
        try {
            String[] p = new String(Base64.getUrlDecoder().decode(cursor), StandardCharsets.UTF_8).split("\\|");
            if (p.length == parts) {
                return p;
            }
        } catch (IllegalArgumentException e) {
            // fall through
        }
        throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_CURSOR", "cursor is not one this API issued.");
    }
}
