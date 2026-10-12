import { test } from "node:test";
import assert from "node:assert/strict";
import {
  HoldMachine,
  LOCK_DISTANCE,
  RAMP_MS,
  RAMP_START,
  clampWpm,
  pauseMultiplier,
  rampFactor,
  sentenceIndexAt,
  splitWords,
  wordDelayMs,
} from "../src/speedread.ts";

test("clampWpm keeps the speed within the allowed range", () => {
  assert.equal(clampWpm(10), 150);
  assert.equal(clampWpm(9000), 800);
  assert.equal(clampWpm(299.6), 300);
});

test("the ramp starts at half speed, reaches full speed after 15 seconds and stays there", () => {
  assert.equal(rampFactor(0), RAMP_START);
  assert.equal(rampFactor(RAMP_MS), 1);
  assert.equal(rampFactor(RAMP_MS * 3), 1);
  // ease-out: more than half of the change happens in the first half of the ramp
  assert.ok(rampFactor(RAMP_MS / 2) > (RAMP_START + 1) / 2);
  let last = 0;
  for (let t = 0; t <= RAMP_MS; t += 500) {
    const f = rampFactor(t);
    assert.ok(f >= last, "never slower as the ramp goes on");
    last = f;
  }
});

test("word delay follows the target speed once the ramp is done", () => {
  assert.equal(wordDelayMs("word", 300, RAMP_MS), 200);
  assert.equal(wordDelayMs("word", 600, RAMP_MS), 100);
});

test("word delay is twice as long at the start and shorter than 15 seconds later", () => {
  assert.equal(wordDelayMs("word", 300, 0), 400);
  assert.ok(wordDelayMs("word", 300, 5_000) < wordDelayMs("word", 300, 0));
});

test("punctuation adds a pause after a word", () => {
  assert.equal(pauseMultiplier("plain"), 1);
  assert.equal(pauseMultiplier("clause,"), 1.2);
  assert.equal(pauseMultiplier("end."), 1.5);
  assert.equal(pauseMultiplier("(end.)"), 1.5);
  assert.equal(pauseMultiplier("“quoted.”"), 1.5);
  assert.equal(wordDelayMs("end.", 300, RAMP_MS), 300);
});

test("splitWords splits on any whitespace and drops empty parts", () => {
  assert.deepEqual(splitWords("  The  model\tfits\n data. "), ["The", "model", "fits", "data."]);
  assert.deepEqual(splitWords("   "), []);
});

test("sentenceIndexAt finds the sentence that is being spoken", () => {
  const sentences = [{ start: 0 }, { start: 3.2 }, { start: 7 }];
  assert.equal(sentenceIndexAt(sentences, -1), -1);
  assert.equal(sentenceIndexAt(sentences, 0), 0);
  assert.equal(sentenceIndexAt(sentences, 3.14), 0);
  assert.equal(sentenceIndexAt(sentences, 3.16), 1); // within the 50 ms tolerance of the next start
  assert.equal(sentenceIndexAt(sentences, 5), 1);
  assert.equal(sentenceIndexAt(sentences, 99), 2);
  assert.equal(sentenceIndexAt([], 4), -1);
});

test("holding starts and lifting stops", () => {
  const m = new HoldMachine();
  assert.equal(m.down(500), "start");
  assert.equal(m.move(498), "none");
  assert.equal(m.up(), "stop");
  assert.equal(m.locked, false);
});

test("sliding up past the lock distance locks, and lifting then keeps reading", () => {
  const m = new HoldMachine();
  m.down(500);
  assert.equal(m.move(500 - LOCK_DISTANCE + 1), "none");
  assert.equal(m.move(500 - LOCK_DISTANCE), "lock");
  assert.equal(m.locked, true);
  assert.equal(m.move(300), "none", "a locked hold locks only once");
  assert.equal(m.up(), "none");
  assert.equal(m.locked, true);
});

test("a tap while locked stops, and the lift of that tap is ignored", () => {
  const m = new HoldMachine();
  m.down(500);
  m.move(400);
  m.up();
  assert.equal(m.down(410), "stop");
  assert.equal(m.locked, false);
  assert.equal(m.up(), "none", "the unlocking tap does not stop a second time");
  // the next press behaves like a fresh hold again
  assert.equal(m.down(410), "start");
  assert.equal(m.up(), "stop");
});

test("cancel behaves like a lift, but does not stop a locked reading", () => {
  const holding = new HoldMachine();
  holding.down(500);
  assert.equal(holding.cancel(), "stop");

  const locked = new HoldMachine();
  locked.down(500);
  locked.move(400);
  assert.equal(locked.cancel(), "none");
  assert.equal(locked.locked, true);
});

test("moving or lifting without a press does nothing", () => {
  const m = new HoldMachine();
  assert.equal(m.move(0), "none");
  assert.equal(m.up(), "none");
  m.reset();
  assert.equal(m.down(1), "start");
});
