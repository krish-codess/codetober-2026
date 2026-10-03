package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;
import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.matchesPattern;
import static org.hamcrest.Matchers.notNullValue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;

/** The HTTP contract: for each endpoint, success, validation failure, authorization failure and malformed input. */
@AutoConfigureMockMvc
class ApiTest extends IntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired ObjectMapper json;

    private String session() throws Exception {
        return body(mvc.perform(post("/api/sessions")).andExpect(status().isCreated())).get("token").asText();
    }

    private JsonNode body(ResultActions r) throws Exception {
        return json.readTree(r.andReturn().getResponse().getContentAsString());
    }

    private static MockHttpServletRequestBuilder as(MockHttpServletRequestBuilder b, String token) {
        return b.header("Authorization", "Bearer " + token);
    }

    private static MockHttpServletRequestBuilder admin(MockHttpServletRequestBuilder b) {
        return b.header("X-Admin-Key", ADMIN_KEY);
    }

    /** Joins the queue and waits to be let in. */
    private String admission(String token, UUID event) throws Exception {
        mvc.perform(as(post("/api/events/{id}/queue", event), token)).andExpect(status().isOk());
        return await().atMost(Duration.ofSeconds(10)).until(
                () -> body(mvc.perform(as(get("/api/events/{id}/queue", event), token))), b -> b.path("state").asText().equals("ADMITTED"))
                .get("admissionToken").asText();
    }

    private ResultActions reserve(String token, String admission, String key, String bodyJson) throws Exception {
        MockHttpServletRequestBuilder b = as(post("/api/reservations"), token).contentType(MediaType.APPLICATION_JSON).content(bodyJson);
        if (admission != null) {
            b.header("X-Admission-Token", admission);
        }
        if (key != null) {
            b.header("Idempotency-Key", key);
        }
        return mvc.perform(b);
    }

    // ---- the primary journey -----------------------------------------------------------------------------------------

    @Test
    void journey_session_queue_admission_hold_confirm() throws Exception {
        UUID section = newSection(3);
        UUID event = inventory.eventOf(section);
        String token = session();

        mvc.perform(get("/api/events/{id}", event)).andExpect(status().isOk()).andExpect(jsonPath("$.serverTime", notNullValue()));
        mvc.perform(get("/api/events/{id}/availability", event)).andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "max-age=1, public"))
                .andExpect(jsonPath("$.sections[0].available").value(3));

        String admission = admission(token, event);
        String reserveBody = "{\"inventoryId\":\"" + section + "\",\"quantity\":2}";
        JsonNode held = body(reserve(token, admission, "journey-key-1", reserveBody).andExpect(status().isCreated())
                .andExpect(jsonPath("$.status").value("HELD")).andExpect(jsonPath("$.expiresAt", notNullValue()))
                .andExpect(jsonPath("$.userId").doesNotExist()));
        String id = held.get("id").asText();

        // A retry of the same request is the same reservation.
        reserve(token, admission, "journey-key-1", reserveBody).andExpect(status().isCreated()).andExpect(jsonPath("$.id").value(id));

        mvc.perform(as(get("/api/reservations/{id}", id), token)).andExpect(status().isOk()).andExpect(jsonPath("$.quantity").value(2));
        mvc.perform(as(post("/api/reservations/{id}/confirm", id), token)).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("CONFIRMED"));
        mvc.perform(as(post("/api/reservations/{id}/confirm", id), token)).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("CONFIRMED"));
        mvc.perform(as(delete("/api/reservations/{id}", id), token)).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("ALREADY_CLOSED"));
        mvc.perform(as(get("/api/reservations"), token)).andExpect(status().isOk()).andExpect(jsonPath("$.items", hasSize(1)))
                .andExpect(jsonPath("$.next").doesNotExist());

        // Second buyer finds one ticket left, takes it back out of their basket, and it is available again.
        String other = session();
        String otherAdmission = admission(other, event);
        reserve(other, otherAdmission, "journey-key-2", reserveBody).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SOLD_OUT")).andExpect(jsonPath("$.detail").value("Only 1 left in this section."))
                .andExpect(jsonPath("$.correlationId", notNullValue()));
        String otherId = body(reserve(other, otherAdmission, "journey-key-3", "{\"inventoryId\":\"" + section + "\",\"quantity\":1}")
                .andExpect(status().isCreated())).get("id").asText();
        mvc.perform(as(delete("/api/reservations/{id}", otherId), other)).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("RELEASED"));
        mvc.perform(as(delete("/api/reservations/{id}", otherId), other)).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("RELEASED"));
        assertThat(position(section)).extracting("held", "sold", "available").containsExactly(0, 2, 1);
    }

    // ---- authentication and authorization ----------------------------------------------------------------------------

    @Test
    void protectedEndpointsRejectMissingForgedAndWrongKindOfCredentials() throws Exception {
        UUID event = inventory.eventOf(newSection(1));
        mvc.perform(post("/api/events/{id}/queue", event)).andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("UNAUTHENTICATED")).andExpect(jsonPath("$.correlationId", notNullValue()));
        mvc.perform(get("/api/reservations")).andExpect(status().isUnauthorized());
        mvc.perform(as(get("/api/reservations"), "not.a.jwt")).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHENTICATED"));
        String token = session();
        String tampered = token.substring(0, token.length() - 2) + (token.endsWith("A") ? "BB" : "AA");
        mvc.perform(as(get("/api/reservations"), tampered)).andExpect(status().isUnauthorized());

        // A shopper is not an operator; neither is a wrong key.
        mvc.perform(as(get("/api/admin/events/{id}/analytics", event), token)).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("FORBIDDEN"));
        mvc.perform(get("/api/admin/events/{id}/analytics", event).header("X-Admin-Key", "wrong")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/admin/dead-letters")).andExpect(status().isUnauthorized());
        mvc.perform(admin(get("/api/admin/events/{id}/analytics", event))).andExpect(status().isOk()).andExpect(jsonPath("$.oversoldTickets").value(0));
    }

    @Test
    void reservingNeedsAdmission_andYouCannotTouchSomeoneElsesReservation() throws Exception {
        UUID section = newSection(5);
        UUID event = inventory.eventOf(section);
        String alice = session(), mallory = session();
        String body = "{\"inventoryId\":\"" + section + "\",\"quantity\":1}";

        reserve(mallory, null, "authz-key-01", body).andExpect(status().isBadRequest()); // header missing
        reserve(mallory, "garbage", "authz-key-01", body).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("NOT_ADMITTED"));
        String aliceAdmission = admission(alice, event);
        reserve(mallory, aliceAdmission, "authz-key-01", body).andExpect(status().isForbidden());

        String id = body(reserve(alice, aliceAdmission, "authz-key-02", body).andExpect(status().isCreated())).get("id").asText();
        mvc.perform(as(get("/api/reservations/{id}", id), mallory)).andExpect(status().isNotFound());
        mvc.perform(as(post("/api/reservations/{id}/confirm", id), mallory)).andExpect(status().isNotFound());
        mvc.perform(as(delete("/api/reservations/{id}", id), mallory)).andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("RESERVATION_NOT_FOUND"));
        mvc.perform(as(get("/api/reservations/{id}", id), alice)).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("HELD"));
    }

    // ---- validation and malformed input ------------------------------------------------------------------------------

    @Test
    void reservationInputIsValidatedAtTheBoundary() throws Exception {
        UUID section = newSection(5);
        String token = session();
        String admission = admission(token, inventory.eventOf(section));

        reserve(token, admission, "valid-key-01", "{\"inventoryId\":\"" + section + "\",\"quantity\":0}").andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_REQUEST")).andExpect(jsonPath("$.errors[0].field").value("quantity"));
        reserve(token, admission, "valid-key-01", "{\"inventoryId\":\"" + section + "\",\"quantity\":11}").andExpect(status().isBadRequest());
        reserve(token, admission, "valid-key-01", "{\"quantity\":1}").andExpect(status().isBadRequest()).andExpect(jsonPath("$.errors[0].field").value("inventoryId"));
        reserve(token, admission, "valid-key-01", "{\"inventoryId\":\"nope\",\"quantity\":1}").andExpect(status().isBadRequest());
        reserve(token, admission, "valid-key-01", "{\"inventoryId\":").andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_REQUEST"));
        reserve(token, admission, null, "{\"inventoryId\":\"" + section + "\",\"quantity\":1}").andExpect(status().isBadRequest());
        reserve(token, admission, "short", "{\"inventoryId\":\"" + section + "\",\"quantity\":1}").andExpect(status().isBadRequest());
        reserve(token, admission, "valid-key-01", "{\"inventoryId\":\"" + UUID.randomUUID() + "\",\"quantity\":1}").andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("INVENTORY_NOT_FOUND"));
        mvc.perform(as(post("/api/reservations"), token).contentType(MediaType.TEXT_PLAIN).content("hi")
                .header("Idempotency-Key", "valid-key-01").header("X-Admission-Token", admission)).andExpect(status().isUnsupportedMediaType());
        assertThat(position(section).held()).isZero();
    }

    @Test
    void readEndpointsHandleUnknownIdsBadIdsAndBadCursors() throws Exception {
        String token = session();
        mvc.perform(get("/api/events/{id}", UUID.randomUUID())).andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("EVENT_NOT_FOUND"));
        mvc.perform(get("/api/events/not-a-uuid")).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_REQUEST"));
        mvc.perform(get("/api/events/{id}/availability", UUID.randomUUID())).andExpect(status().isNotFound());
        mvc.perform(as(get("/api/events/{id}/queue", UUID.randomUUID()), token)).andExpect(status().isNotFound());
        mvc.perform(as(get("/api/reservations/{id}", UUID.randomUUID()), token)).andExpect(status().isNotFound());
        mvc.perform(as(get("/api/reservations/not-a-uuid"), token)).andExpect(status().isBadRequest());
        mvc.perform(as(get("/api/reservations").param("cursor", "!!!"), token)).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_CURSOR"));
        mvc.perform(as(get("/api/reservations").param("limit", "0"), token)).andExpect(status().isBadRequest());
        mvc.perform(as(get("/api/reservations").param("limit", "1000"), token)).andExpect(status().isBadRequest());
        mvc.perform(get("/api/events").param("limit", "abc")).andExpect(status().isBadRequest());
        mvc.perform(as(get("/api/events/{id}/queue", UUID.randomUUID()), token).header("X-Request-Id", "my-trace-id-0001"))
                .andExpect(header().string("X-Request-Id", "my-trace-id-0001")).andExpect(jsonPath("$.correlationId").value("my-trace-id-0001"));
        mvc.perform(get("/api/events/{id}", UUID.randomUUID()).header("X-Request-Id", "bad id\twith spaces"))
                .andExpect(header().string("X-Request-Id", matchesPattern("[0-9a-f-]{36}")));
    }

    @Test
    void listsPaginateWithAStableCursor() throws Exception {
        for (int i = 0; i < 3; i++) {
            newSection(1);
        }
        JsonNode first = body(mvc.perform(get("/api/events").param("limit", "2")).andExpect(status().isOk()).andExpect(jsonPath("$.items", hasSize(2))));
        JsonNode second = body(mvc.perform(get("/api/events").param("limit", "2").param("cursor", first.get("next").asText())).andExpect(status().isOk()));
        assertThat(second.get("items").get(0).get("id")).isNotEqualTo(first.get("items").get(1).get("id")).isNotEqualTo(first.get("items").get(0).get("id"));
        assertThat(Instant.parse(second.get("items").get(0).get("onSaleAt").asText()))
                .isAfterOrEqualTo(Instant.parse(first.get("items").get(1).get("onSaleAt").asText()));
    }

    // ---- operator endpoints ------------------------------------------------------------------------------------------

    @Test
    void operatorCanCreateAnEventIdempotently_adjustStock_readTheStream_andTimeTravel() throws Exception {
        UUID event = UUID.randomUUID();
        String spec = """
                {"name":"Api test","onSaleAt":"2020-01-01T00:00:00Z","holdSeconds":60,"maxPerUser":4,"admissionRatePerSec":10,
                 "inventory":[{"ticketType":"GA","section":"FLOOR","priceCents":5000,"total":10}]}""";
        mvc.perform(admin(put("/api/admin/events/{id}", event)).contentType(MediaType.APPLICATION_JSON).content(spec)).andExpect(status().isCreated());
        mvc.perform(admin(put("/api/admin/events/{id}", event)).contentType(MediaType.APPLICATION_JSON).content(spec)).andExpect(status().isOk());
        String section = body(mvc.perform(get("/api/events/{id}/availability", event))).get("sections").get(0).get("inventoryId").asText();
        assertThat(body(mvc.perform(get("/api/events/{id}/availability", event))).get("sections")).hasSize(1);

        mvc.perform(admin(put("/api/admin/events/{id}", UUID.randomUUID())).contentType(MediaType.APPLICATION_JSON)
                        .content(spec.replace("\"total\":10", "\"total\":0").replace("\"FLOOR\"", "\"floor!\"")))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.errors", hasSize(2)));
        mvc.perform(admin(put("/api/admin/events/{id}", UUID.randomUUID())).contentType(MediaType.APPLICATION_JSON).content("{"))
                .andExpect(status().isBadRequest());

        String adjust = "{\"delta\":5,\"expectedVersion\":1,\"reason\":\"release\"}";
        mvc.perform(admin(post("/api/admin/inventory/{id}/adjustments", section)).contentType(MediaType.APPLICATION_JSON).content(adjust))
                .andExpect(status().isOk()).andExpect(jsonPath("$.total").value(15)).andExpect(jsonPath("$.version").value(2));
        mvc.perform(admin(post("/api/admin/inventory/{id}/adjustments", section)).contentType(MediaType.APPLICATION_JSON).content(adjust))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("VERSION_MISMATCH"));
        mvc.perform(admin(post("/api/admin/inventory/{id}/adjustments", section)).contentType(MediaType.APPLICATION_JSON)
                        .content("{\"delta\":-99,\"expectedVersion\":2,\"reason\":\"oops\"}"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("CORRECTION_BELOW_COMMITTED"));
        mvc.perform(admin(post("/api/admin/inventory/{id}/adjustments", section)).contentType(MediaType.APPLICATION_JSON).content("{\"delta\":5}"))
                .andExpect(status().isBadRequest());
        mvc.perform(admin(post("/api/admin/inventory/{id}/adjustments", UUID.randomUUID())).contentType(MediaType.APPLICATION_JSON).content(adjust))
                .andExpect(status().isNotFound());

        JsonNode page = body(mvc.perform(admin(get("/api/admin/inventory/{id}/events", section)).param("limit", "1")).andExpect(status().isOk())
                .andExpect(jsonPath("$.items[0].type").value("STOCK_ADDED")).andExpect(jsonPath("$.items[0].reason").value("opening stock")));
        mvc.perform(admin(get("/api/admin/inventory/{id}/events", section)).param("cursor", page.get("next").asText())).andExpect(status().isOk())
                .andExpect(jsonPath("$.items", hasSize(1))).andExpect(jsonPath("$.items[0].totalDelta").value(5)).andExpect(jsonPath("$.next").doesNotExist());

        mvc.perform(admin(get("/api/admin/events/{id}/position", event)).param("at", Instant.now().plusSeconds(1).toString())).andExpect(status().isOk())
                .andExpect(jsonPath("$[0].total").value(15));
        mvc.perform(admin(get("/api/admin/events/{id}/position", event)).param("at", "2019-01-01T00:00:00Z")).andExpect(status().isOk())
                .andExpect(jsonPath("$[0].total").value(0));
        mvc.perform(admin(get("/api/admin/events/{id}/position", event)).param("at", "last tuesday")).andExpect(status().isBadRequest());
        mvc.perform(admin(get("/api/admin/events/{id}/position", event))).andExpect(status().isBadRequest());
        mvc.perform(admin(get("/api/admin/events/{id}/position", UUID.randomUUID())).param("at", "2019-01-01T00:00:00Z")).andExpect(status().isNotFound());
        mvc.perform(admin(get("/api/admin/events/{id}/analytics", UUID.randomUUID()))).andExpect(status().isNotFound());
        mvc.perform(admin(get("/api/admin/dead-letters")).param("limit", "5")).andExpect(status().isOk());
        mvc.perform(admin(get("/api/admin/dead-letters")).param("cursor", "bm9wZQ")).andExpect(status().isBadRequest());
        mvc.perform(admin(post("/api/admin/dead-letters/{id}/replay", 999_999_999))).andExpect(status().isNotFound());
        mvc.perform(admin(post("/api/admin/dead-letters/{id}/replay", "x"))).andExpect(status().isBadRequest());
    }

    @Test
    void openApiDocumentIsGeneratedFromTheCode() throws Exception {
        mvc.perform(get("/v3/api-docs")).andExpect(status().isOk())
                .andExpect(jsonPath("$.paths['/api/reservations'].post.summary").value("Hold tickets"))
                .andExpect(jsonPath("$.components.securitySchemes.session.scheme").value("bearer"));
    }
}
