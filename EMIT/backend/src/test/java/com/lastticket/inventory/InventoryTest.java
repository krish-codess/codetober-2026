package com.lastticket.inventory;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.lastticket.api.ApiException;
import com.lastticket.inventory.Inventory.Change;
import com.lastticket.inventory.Inventory.EventType;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class InventoryTest {

    private static Inventory of(int total, int held, int sold) {
        return new Inventory(UUID.randomUUID(), UUID.randomUUID(), "GA", "FLOOR", 5000, 7, total, held, sold);
    }

    @Test
    void holdTakesFromAvailableAndBumpsVersion() {
        Inventory inv = of(10, 3, 2);
        Inventory after = inv.apply(inv.placeHold(5));
        assertThat(after.held()).isEqualTo(8);
        assertThat(after.available()).isZero();
        assertThat(after.version()).isEqualTo(8);
    }

    @Test
    void holdBeyondAvailableIsSoldOut() {
        assertThatThrownBy(() -> of(10, 3, 2).placeHold(6)).isInstanceOf(ApiException.class)
                .hasMessageContaining("Only 5 left").extracting("code").isEqualTo("SOLD_OUT");
        assertThatThrownBy(() -> of(10, 6, 4).placeHold(1)).hasMessageContaining("No tickets left");
    }

    @Test
    void nonPositiveQuantityIsRejected() {
        assertThatThrownBy(() -> of(10, 0, 0).placeHold(0)).extracting("code").isEqualTo("INVALID_QUANTITY");
        assertThatThrownBy(() -> of(10, 0, 0).placeHold(-1)).extracting("code").isEqualTo("INVALID_QUANTITY");
    }

    @Test
    void confirmMovesHeldToSoldAndExpiryOrReleaseGivesItBack() {
        Inventory inv = of(10, 4, 1);
        assertThat(inv.apply(inv.confirmHold(4))).extracting(Inventory::held, Inventory::sold, Inventory::available).containsExactly(0, 5, 5);
        assertThat(inv.apply(inv.expireHold(4))).extracting(Inventory::held, Inventory::sold, Inventory::available).containsExactly(0, 1, 9);
        assertThat(inv.apply(inv.releaseHold(1))).extracting(Inventory::held, Inventory::sold, Inventory::available).containsExactly(3, 1, 6);
    }

    @Test
    void closingMoreThanIsHeldIsABugNotABusinessError() {
        assertThatThrownBy(() -> of(10, 1, 0).confirmHold(2)).isInstanceOf(IllegalStateException.class);
    }

    @Test
    void stockCanBeAddedAndCorrectedDownToWhatIsCommittedButNoFurther() {
        Inventory inv = of(10, 3, 2);
        assertThat(inv.adjustStock(5)).isEqualTo(new Change(EventType.STOCK_ADDED, 5, 0, 0));
        assertThat(inv.apply(inv.adjustStock(-5)).total()).isEqualTo(5); // exactly held + sold: holds intact
        assertThatThrownBy(() -> inv.adjustStock(-6)).extracting("code").isEqualTo("CORRECTION_BELOW_COMMITTED");
        assertThatThrownBy(() -> inv.adjustStock(0)).extracting("code").isEqualTo("INVALID_DELTA");
    }

    /** Whatever sequence of commands is thrown at it, the aggregate never promises more than it has. */
    @Test
    void invariantHoldsUnderRandomCommandSequences() {
        Random random = new Random(20261010);
        for (int run = 0; run < 200; run++) {
            Inventory inv = of(random.nextInt(50), 0, 0);
            List<Integer> holds = new ArrayList<>();
            for (int step = 0; step < 300; step++) {
                try {
                    switch (random.nextInt(5)) {
                        case 0 -> {
                            int q = 1 + random.nextInt(4);
                            inv = inv.apply(inv.placeHold(q));
                            holds.add(q);
                        }
                        case 1 -> { if (!holds.isEmpty()) inv = inv.apply(inv.confirmHold(holds.remove(random.nextInt(holds.size())))); }
                        case 2 -> { if (!holds.isEmpty()) inv = inv.apply(inv.expireHold(holds.remove(random.nextInt(holds.size())))); }
                        case 3 -> { if (!holds.isEmpty()) inv = inv.apply(inv.releaseHold(holds.remove(random.nextInt(holds.size())))); }
                        default -> inv = inv.apply(inv.adjustStock(random.nextInt(21) - 10));
                    }
                } catch (ApiException expectedRefusal) {
                    // refusals are fine; breaking the invariant is not
                }
                assertThat(inv.held()).isEqualTo(holds.stream().mapToInt(Integer::intValue).sum());
                assertThat(inv.held()).isNotNegative();
                assertThat(inv.sold()).isNotNegative();
                assertThat(inv.held() + inv.sold()).isLessThanOrEqualTo(inv.total());
            }
        }
    }
}
