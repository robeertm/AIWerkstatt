# Queue priority

Waiting orders start in a fixed order. By default that is first come, first served by task id. An administrator can change the
order from the **Agents at work** overview; the rule lives in one place, `backend/queue_priority.py`, and both the orchestrator
(who gets the next free slot) and the overview (order, position, expected start) use it.

## Rules

- No priority set: strictly by task id (9 before 10). Control messages (stop, compact) are never part of the order.
- **Urgent** always stands before **normal**; inside a class it stays first come, first served.
- **Next** (front of the class), **up**, **down**, **to the end** and **to position n** move an order only inside its own class
  (urgent or normal). At the edge nothing happens.
- An order with several waiting parts (the order and follow-ups of the same thread) moves as one block.
- Priority acts on **waiting** orders only. A running order is never touched or restarted; the priority only decides who gets
  the next free slot. Entries of orders that no longer wait are removed with every change.
- A manual position is a number between the new neighbours (default: the task id), so only the moved block gets a new rank.

## API (administrators)

`POST /api/queue/<thread id>/priority`

```json
{"action": "urgent"}
{"action": "position", "position": 2}
```

Actions: `urgent`, `normal`, `front`, `up`, `down`, `end`, `position`. The answer carries `before`, `after`, `urgent` and
`unchanged`. `409` = the order does not wait (running, finished or unknown), `400` = unknown action or bad position,
`403` for members and API tokens, `401` without a session.

`GET /api/pool` shows the result: `queue` (one row per waiting order in start order with `position`, `urgent`, `class_position`,
`can_up`, `can_down`, `followups`, `title`) and `waiting_list` in start order.

## Files

- `data/queue-priority.json` - the current state, written atomically under a file lock. Missing or broken means plain first come,
  first served.
- `data/queue-priority.jsonl` - one line per change (who, when, action, position before and after).

## Overview

The **Queue** list under the worker slots shows the start order. Each row has buttons of at least 44 px (urgent / normal, next,
up, down). On a desktop the rows can also be dragged onto another row of the same class. Tests:
`cd backend && python3 -m unittest tests.test_queue_priority`.
