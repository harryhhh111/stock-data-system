import assert from 'node:assert/strict';
import test from 'node:test';

import { dayHeader } from './news-page';


test('日期分组星期不受浏览器本地时区影响', () => {
  const previous = process.env.TZ;
  try {
    for (const timezone of ['UTC', 'Asia/Shanghai', 'America/Los_Angeles']) {
      process.env.TZ = timezone;
      assert.equal(
        dayHeader('2026-10-26', 2),
        '2026-10-26 星期一 · 2 条（北京时间）',
        timezone,
      );
    }
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
});
