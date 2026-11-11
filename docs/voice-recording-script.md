# Debrief voice recording script

Read these lines to make your own voice for the live coach. How to say them: calm, clear, like a race engineer on the radio. Not too fast, the same pace and the same distance from the microphone all the way through.

**Recording tips**

- A quiet room, phone or headset microphone about a hand's width from your mouth.
- Save as WAV if your recorder app can (FLAC, MP3 or M4A also work when ffmpeg is installed).
- One long recording: read every line in order and leave a clear pause (count "one, two" in your head) between lines. A comma inside a line gets a short, natural pause, much shorter than the pause between lines.
- Made a mistake? Pause, then read the line again, and fix it later by recording just that line as its own file named by its number (for example `57.wav`).
- You can split the reading into several recordings, one per section; tell the importer where each one starts (`--long 1:part1.wav --long 59:part2.wav`).

Then make the pack:

```
python tools/import_recordings.py --long myvoice.wav --id myvoice --name "My voice" --by "Your name"
```


## Section A (from line 1)

Brake markers. One clear phrase, a small pause at the comma.

1. Brake marker, turn one.
2. Brake marker, turn two.
3. Brake marker, turn three.
4. Brake marker, turn four.
5. Brake marker, turn five.
6. Brake marker, turn six.
7. Brake marker, turn seven.
8. Brake marker, turn eight.
9. Brake marker, turn nine.
10. Brake marker, turn ten.
11. Brake marker, turn eleven.
12. Brake marker, turn twelve.
13. Brake marker, turn thirteen.
14. Brake marker, turn fourteen.
15. Brake marker, turn fifteen.
16. Brake marker, turn sixteen.
17. Brake marker, turn seventeen.
18. Brake marker, turn eighteen.
19. Brake marker, turn nineteen.
20. Brake marker, turn twenty.
21. Brake marker, turn twenty-one.
22. Brake marker, turn twenty-two.
23. Brake marker, turn twenty-three.
24. Brake marker, turn twenty-four.
25. Brake marker, turn twenty-five.

## Section B (from line 26)

Corner tips. Say the turn, a short natural pause at the comma, then the tip.

26. Turn one, carry more speed.
27. Turn two, lighter brake.
28. Turn three, brake later.
29. Turn four, brake earlier.
30. Turn five, trail into it.
31. Turn six, no coasting.
32. Turn seven, ease off slower.
33. Turn eight, throttle earlier.
34. Turn nine, smoother throttle.
35. Turn ten, smoother brake.
36. Turn eleven, hold it in.
37. Turn twelve, same every lap.
38. Turn thirteen, braked early.
39. Turn fourteen, more speed, four down.
40. Turn fifteen, more speed, five down.
41. Turn sixteen, more speed, six down.
42. Turn seventeen, more speed, seven down.
43. Turn eighteen, more speed, eight down.
44. Turn nineteen, more speed, nine down.
45. Turn twenty, more speed, ten down.
46. Turn twenty-one, more speed, eleven down.
47. Turn twenty-two, more speed, twelve down.
48. Turn twenty-three, more speed, thirteen down.
49. Turn twenty-four, more speed, fourteen down.
50. Turn twenty-five, more speed, fifteen down.
51. Turn one, more speed, sixteen down.
52. Turn two, more speed, seventeen down.
53. Turn three, more speed, eighteen down.
54. Turn four, more speed, nineteen down.
55. Turn five, more speed, twenty down.

## Section C (from line 56)

Lap times, slower than your best. Flat and matter-of-fact.

56. Down a tenth.
57. Down two tenths.
58. Down three tenths.
59. Down four tenths.
60. Down five tenths.
61. Down six tenths.
62. Down seven tenths.
63. Down eight tenths.
64. Down nine tenths.
65. Down one second.
66. Down one point one seconds.
67. Down one point two seconds.
68. Down one point three seconds.
69. Down one point four seconds.
70. Down one point five seconds.
71. Down one point six seconds.
72. Down one point seven seconds.
73. Down one point eight seconds.
74. Down one point nine seconds.
75. Down two seconds.

## Section D (from line 76)

Lap times, a new best. A little lift, but stay calm. Short pause at the comma.

76. New best, up a tenth.
77. New best, up two tenths.
78. New best, up three tenths.
79. New best, up four tenths.
80. New best, up five tenths.
81. New best, up six tenths.
82. New best, up seven tenths.
83. New best, up eight tenths.
84. New best, up nine tenths.
85. New best, up one second.
86. New best, up one point one seconds.
87. New best, up one point two seconds.
88. New best, up one point three seconds.
89. New best, up one point four seconds.
90. New best, up one point five seconds.
91. New best, up one point six seconds.
92. New best, up one point seven seconds.
93. New best, up one point eight seconds.
94. New best, up one point nine seconds.
95. New best, up two seconds.

## Section E (from line 96)

Brake pressure. First 'Good, eighty-four' with a short pause at the comma, then each number on its own.

96. Good, eighty-four.
97. Twenty.
98. Twenty-one.
99. Twenty-two.
100. Twenty-three.
101. Twenty-four.
102. Twenty-five.
103. Twenty-six.
104. Twenty-seven.
105. Twenty-eight.
106. Twenty-nine.
107. Thirty.
108. Thirty-one.
109. Thirty-two.
110. Thirty-three.
111. Thirty-four.
112. Thirty-five.
113. Thirty-six.
114. Thirty-seven.
115. Thirty-eight.
116. Thirty-nine.
117. Forty.
118. Forty-one.
119. Forty-two.
120. Forty-three.
121. Forty-four.
122. Forty-five.
123. Forty-six.
124. Forty-seven.
125. Forty-eight.
126. Forty-nine.
127. Fifty.
128. Fifty-one.
129. Fifty-two.
130. Fifty-three.
131. Fifty-four.
132. Fifty-five.
133. Fifty-six.
134. Fifty-seven.
135. Fifty-eight.
136. Fifty-nine.
137. Sixty.
138. Sixty-one.
139. Sixty-two.
140. Sixty-three.
141. Sixty-four.
142. Sixty-five.
143. Sixty-six.
144. Sixty-seven.
145. Sixty-eight.
146. Sixty-nine.
147. Seventy.
148. Seventy-one.
149. Seventy-two.
150. Seventy-three.
151. Seventy-four.
152. Seventy-five.
153. Seventy-six.
154. Seventy-seven.
155. Seventy-eight.
156. Seventy-nine.
157. Eighty.
158. Eighty-one.
159. Eighty-two.
160. Eighty-three.
161. Eighty-four.
162. Eighty-five.
163. Eighty-six.
164. Eighty-seven.
165. Eighty-eight.
166. Eighty-nine.
167. Ninety.
168. Ninety-one.
169. Ninety-two.
170. Ninety-three.
171. Ninety-four.
172. Ninety-five.
173. Ninety-six.
174. Ninety-seven.
175. Ninety-eight.
176. Ninety-nine.
177. One hundred.

## Section F (from line 178)

Short practice cues. Each one on its own.

178. Good brake point.
179. Good, held it in.
180. Good, no coasting.
181. Good throttle.
182. Good speed.
183. Good, on pace.
184. Too much brake.
185. Braked too early.
186. Braked too late.
187. Hold the brake in.
188. Less coasting.
189. More speed.
190. New best.
191. Building your reference.

## Section G (from line 192)

Time in a corner or a sector. Say the turn or sector, a short pause at the comma, then the time.

192. Turn four, a tenth down.
193. Turn five, two tenths down.
194. Turn six, three tenths down.
195. Turn seven, four tenths down.
196. Turn eight, five tenths down.
197. Turn nine, six tenths down.
198. Turn ten, seven tenths down.
199. Turn eleven, eight tenths down.
200. Turn twelve, nine tenths down.
201. Turn thirteen, one second down.
202. Turn fourteen, one point one seconds down.
203. Turn fifteen, one point two seconds down.
204. Turn sixteen, one point three seconds down.
205. Turn seventeen, one point four seconds down.
206. Turn eighteen, one point five seconds down.
207. Turn nineteen, one point six seconds down.
208. Turn twenty, one point seven seconds down.
209. Turn twenty-one, one point eight seconds down.
210. Turn twenty-two, one point nine seconds down.
211. Turn twenty-three, two seconds down.
212. Sector one, up two tenths.
213. Sector two, up three tenths.
214. Sector three, up four tenths.

## Section H (from line 215)

Lap times. Like reading a stopwatch: a short pause at the comma before the last number.

215. Reference set, one thirty-eight nine.
216. Forty, oh.
217. Forty-one, seven.
218. Forty-two, four.
219. Forty-three, one.
220. Forty-four, eight.
221. Forty-five, five.
222. Forty-six, two.
223. Forty-seven, nine.
224. Forty-eight, six.
225. Forty-nine, three.
226. Fifty, oh.
227. Fifty-one, seven.
228. Fifty-two, four.
229. Fifty-three, one.
230. Fifty-four, eight.
231. Fifty-five, five.
232. Fifty-six, two.
233. Fifty-seven, nine.
234. Fifty-eight, six.
235. Fifty-nine, three.
236. One oh oh, oh.
237. One oh one, seven.
238. One oh two, four.
239. One oh three, one.
240. One oh four, eight.
241. One oh five, five.
242. One oh six, two.
243. One oh seven, nine.
244. One oh eight, six.
245. One oh nine, three.
246. One ten, oh.
247. One eleven, seven.
248. One twelve, four.
249. One thirteen, one.
250. One fourteen, eight.
251. One fifteen, five.
252. One sixteen, two.
253. One seventeen, nine.
254. One eighteen, six.
255. One nineteen, three.
256. One twenty, oh.
257. One twenty-one, seven.
258. One twenty-two, four.
259. One twenty-three, one.
260. One twenty-four, eight.
261. One twenty-five, five.
262. One twenty-six, two.
263. One twenty-seven, nine.
264. One twenty-eight, six.
265. One twenty-nine, three.
266. One thirty, oh.
267. One thirty-one, seven.
268. One thirty-two, four.
269. One thirty-three, one.
270. One thirty-four, eight.
271. One thirty-five, five.
272. One thirty-six, two.
273. One thirty-seven, nine.
274. One thirty-eight, six.
275. One thirty-nine, three.
276. One forty, oh.
277. One forty-one, seven.
278. One forty-two, four.
279. One forty-three, one.
280. One forty-four, eight.
281. One forty-five, five.
282. One forty-six, two.
283. One forty-seven, nine.
284. One forty-eight, six.
285. One forty-nine, three.
286. One fifty, oh.
287. One fifty-one, seven.
288. One fifty-two, four.
289. One fifty-three, one.
290. One fifty-four, eight.
291. One fifty-five, five.
292. One fifty-six, two.
293. One fifty-seven, nine.
294. One fifty-eight, six.
295. One fifty-nine, three.
296. Two oh oh, oh.
297. Two oh one, seven.
298. Two oh two, four.
299. Two oh three, one.
300. Two oh four, eight.
301. Two oh five, five.
302. Two oh six, two.
303. Two oh seven, nine.
304. Two oh eight, six.
305. Two oh nine, three.
306. Two ten, oh.
307. Two eleven, seven.
308. Two twelve, four.
309. Two thirteen, one.
310. Two fourteen, eight.
311. Two fifteen, five.
312. Two sixteen, two.
313. Two seventeen, nine.
314. Two eighteen, six.
315. Two nineteen, three.
316. Two twenty, oh.
317. Two twenty-one, seven.
318. Two twenty-two, four.
319. Two twenty-three, one.
320. Two twenty-four, eight.
321. Two twenty-five, five.
322. Two twenty-six, two.
323. Two twenty-seven, nine.
324. Two twenty-eight, six.
325. Two twenty-nine, three.
326. Two thirty, oh.
327. Two thirty-one, seven.
328. Two thirty-two, four.
329. Two thirty-three, one.
330. Two thirty-four, eight.
331. Two thirty-five, five.
332. Two thirty-six, two.
333. Two thirty-seven, nine.
334. Two thirty-eight, six.
335. Two thirty-nine, three.
336. Two forty, oh.
337. Two forty-one, seven.
338. Two forty-two, four.
339. Two forty-three, one.
340. Two forty-four, eight.
341. Two forty-five, five.
342. Two forty-six, two.
343. Two forty-seven, nine.
344. Two forty-eight, six.
345. Two forty-nine, three.
346. Two fifty, oh.
347. Two fifty-one, seven.
348. Two fifty-two, four.
349. Two fifty-three, one.
350. Two fifty-four, eight.
351. Two fifty-five, five.
352. Two fifty-six, two.
353. Two fifty-seven, nine.
354. Two fifty-eight, six.
355. Two fifty-nine, three.
356. Three oh oh, oh.

## Section I (from line 357)

Your pace, when you ask for it. Matter-of-fact, a short natural pause at the comma.

357. Down a tenth, most in turn one.
358. Down two tenths, most in turn two.
359. Down three tenths, most in turn three.
360. Down four tenths, most in turn four.
361. Down five tenths, most in turn five.
362. Down six tenths, most in turn six.
363. Down seven tenths, most in turn seven.
364. Down eight tenths, most in turn eight.
365. Down nine tenths, most in turn nine.
366. Down one second, most in turn ten.
367. Down one point one seconds, most in turn eleven.
368. Down one point two seconds, most in turn twelve.
369. Down one point three seconds, most in turn thirteen.
370. Down one point four seconds, most in turn fourteen.
371. Down one point five seconds, most in turn fifteen.
372. Down one point six seconds, most in turn sixteen.
373. Down one point seven seconds, most in turn seventeen.
374. Down one point eight seconds, most in turn eighteen.
375. Down one point nine seconds, most in turn nineteen.
376. Down two seconds, most in turn twenty.
377. Down a tenth, most in turn twenty-one.
378. Down two tenths, most in turn twenty-two.
379. Down three tenths, most in turn twenty-three.
380. Down four tenths, most in turn twenty-four.
381. Down five tenths, most in turn twenty-five.
382. No clean lap yet.
383. Down more than two seconds.

## Section J (from line 384)

Short lap times, without the minute. Like reading a stopwatch: a short pause at the comma.

384. Oh oh, oh.
385. Oh one, three.
386. Oh two, six.
387. Oh three, nine.
388. Oh four, two.
389. Oh five, five.
390. Oh six, eight.
391. Oh seven, one.
392. Oh eight, four.
393. Oh nine, seven.
394. Ten, oh.
395. Eleven, three.
396. Twelve, six.
397. Thirteen, nine.
398. Fourteen, two.
399. Fifteen, five.
400. Sixteen, eight.
401. Seventeen, one.
402. Eighteen, four.
403. Nineteen, seven.
404. Twenty, oh.
405. Twenty-one, three.
406. Twenty-two, six.
407. Twenty-three, nine.
408. Twenty-four, two.
409. Twenty-five, five.
410. Twenty-six, eight.
411. Twenty-seven, one.
412. Twenty-eight, four.
413. Twenty-nine, seven.
414. Thirty, oh.
415. Thirty-one, three.
416. Thirty-two, six.
417. Thirty-three, nine.
418. Thirty-four, two.
419. Thirty-five, five.
420. Thirty-six, eight.
421. Thirty-seven, one.
422. Thirty-eight, four.
423. Thirty-nine, seven.
424. Forty, oh.
425. Forty-one, three.
426. Forty-two, six.
427. Forty-three, nine.
428. Forty-four, two.
429. Forty-five, five.
430. Forty-six, eight.
431. Forty-seven, one.
432. Forty-eight, four.
433. Forty-nine, seven.
434. Fifty, oh.
435. Fifty-one, three.
436. Fifty-two, six.
437. Fifty-three, nine.
438. Fifty-four, two.
439. Fifty-five, five.
440. Fifty-six, eight.
441. Fifty-seven, one.
442. Fifty-eight, four.
443. Fifty-nine, seven.

## Section K (from line 444)

One more corner tip. Say the turn, a short natural pause at the comma, then the tip.

444. Turn eight, more speed, three down.

## Section L (from line 445)

Learning the track, against your best so far. Say the turn, a short natural pause at the comma, then the rest.

445. Turn four, three down on your best.
446. Turn five, four down on your best.
447. Turn six, five down on your best.
448. Turn seven, six down on your best.
449. Turn eight, seven down on your best.
450. Turn nine, eight down on your best.
451. Turn ten, nine down on your best.
452. Turn eleven, ten down on your best.
453. Turn twelve, eleven down on your best.
454. Turn thirteen, twelve down on your best.
455. Turn fourteen, thirteen down on your best.
456. Turn fifteen, fourteen down on your best.
457. Turn sixteen, fifteen down on your best.
458. Turn seventeen, sixteen down on your best.
459. Turn eighteen, seventeen down on your best.
460. Turn nineteen, eighteen down on your best.
461. Turn twenty, nineteen down on your best.
462. Turn twenty-one, twenty down on your best.
463. Turn three, off track.
464. Turn four, off track and three faster in.
465. Turn five, off track and four faster in.
466. Turn six, off track and five faster in.
467. Turn seven, off track and six faster in.
468. Turn eight, off track and seven faster in.
469. Turn nine, off track and eight faster in.
470. Turn ten, off track and nine faster in.
471. Turn eleven, off track and ten faster in.
472. Turn twelve, off track and eleven faster in.
473. Turn thirteen, off track and twelve faster in.
474. Turn fourteen, off track and thirteen faster in.
475. Turn fifteen, off track and fourteen faster in.
476. Turn sixteen, off track and fifteen faster in.
477. Turn seventeen, off track and sixteen faster in.
478. Turn eighteen, off track and seventeen faster in.
479. Turn nineteen, off track and eighteen faster in.
480. Turn twenty, off track and nineteen faster in.
481. Turn twenty-one, off track and twenty faster in.
482. Turn one, braked ten metres later.
483. Turn two, braked fifteen metres later.
484. Turn three, braked twenty metres later.
485. Turn four, braked twenty-five metres later.
486. Turn five, braked thirty metres later.
487. Turn six, braked thirty-five metres later.
488. Turn seven, braked forty metres later.
489. Turn eight, braked forty-five metres later.
490. Turn nine, braked fifty metres later.
491. Turn ten, braked fifty-five metres later.
492. Turn eleven, braked sixty metres later.
493. Turn one, braked ten metres earlier.
494. Turn two, braked fifteen metres earlier.
495. Turn three, braked twenty metres earlier.
496. Turn four, braked twenty-five metres earlier.
497. Turn five, braked thirty metres earlier.
498. Turn six, braked thirty-five metres earlier.
499. Turn seven, braked forty metres earlier.
500. Turn eight, braked forty-five metres earlier.
501. Turn nine, braked fifty metres earlier.
502. Turn ten, braked fifty-five metres earlier.
503. Turn eleven, braked sixty metres earlier.
504. Turn five, on your best.

## Section M (from line 505)

Crew chief on the radio. Calm and clear, like an engineer telling you what to do next. Numbers are litres, gallons, laps or tenths of a second. A short natural pause at the comma.

505. Data lost.
506. Data back.
507. Fuel critical.
508. Box this lap.
509. Box next lap.
510. Box now, cheap stop.
511. Car alongside.
512. Car ahead pitting.
513. On plan.
514. Ahead of plan.
515. Behind plan.
516. Up a place.
517. Lost a place.
518. Fuel is tight.
519. Short of the flag.
520. Save fuel or box next lap.
521. Box in two laps.
522. Box in three laps.
523. Box in four laps.
524. Box in five laps.
525. One litre.
526. Two litres.
527. Three litres.
528. Four litres.
529. Five litres.
530. Six litres.
531. Seven litres.
532. Eight litres.
533. Nine litres.
534. Ten litres.
535. Eleven litres.
536. Twelve litres.
537. Thirteen litres.
538. Fourteen litres.
539. Fifteen litres.
540. Sixteen litres.
541. Seventeen litres.
542. Eighteen litres.
543. Nineteen litres.
544. Twenty litres.
545. Twenty-one litres.
546. Twenty-two litres.
547. Twenty-three litres.
548. Twenty-four litres.
549. Twenty-five litres.
550. Twenty-six litres.
551. Twenty-seven litres.
552. Twenty-eight litres.
553. Twenty-nine litres.
554. Thirty litres.
555. Thirty-one litres.
556. Thirty-two litres.
557. Thirty-three litres.
558. Thirty-four litres.
559. Thirty-five litres.
560. Thirty-six litres.
561. Thirty-seven litres.
562. Thirty-eight litres.
563. Thirty-nine litres.
564. Forty litres.
565. Forty-one litres.
566. Forty-two litres.
567. Forty-three litres.
568. Forty-four litres.
569. Forty-five litres.
570. Forty-six litres.
571. Forty-seven litres.
572. Forty-eight litres.
573. Forty-nine litres.
574. Fifty litres.
575. Fifty-one litres.
576. Fifty-two litres.
577. Fifty-three litres.
578. Fifty-four litres.
579. Fifty-five litres.
580. Fifty-six litres.
581. Fifty-seven litres.
582. Fifty-eight litres.
583. Fifty-nine litres.
584. Sixty litres.
585. Sixty-one litres.
586. Sixty-two litres.
587. Sixty-three litres.
588. Sixty-four litres.
589. Sixty-five litres.
590. Sixty-six litres.
591. Sixty-seven litres.
592. Sixty-eight litres.
593. Sixty-nine litres.
594. Seventy litres.
595. Seventy-one litres.
596. Seventy-two litres.
597. Seventy-three litres.
598. Seventy-four litres.
599. Seventy-five litres.
600. Seventy-six litres.
601. Seventy-seven litres.
602. Seventy-eight litres.
603. Seventy-nine litres.
604. Eighty litres.
605. Eighty-one litres.
606. Eighty-two litres.
607. Eighty-three litres.
608. Eighty-four litres.
609. Eighty-five litres.
610. Eighty-six litres.
611. Eighty-seven litres.
612. Eighty-eight litres.
613. Eighty-nine litres.
614. Ninety litres.
615. Ninety-one litres.
616. Ninety-two litres.
617. Ninety-three litres.
618. Ninety-four litres.
619. Ninety-five litres.
620. Ninety-six litres.
621. Ninety-seven litres.
622. Ninety-eight litres.
623. Ninety-nine litres.
624. One hundred litres.
625. One hundred and one litres.
626. One hundred and two litres.
627. One hundred and three litres.
628. One hundred and four litres.
629. One hundred and five litres.
630. One hundred and six litres.
631. One hundred and seven litres.
632. One hundred and eight litres.
633. One hundred and nine litres.
634. One hundred and ten litres.
635. One hundred and eleven litres.
636. One hundred and twelve litres.
637. One hundred and thirteen litres.
638. One hundred and fourteen litres.
639. One hundred and fifteen litres.
640. One hundred and sixteen litres.
641. One hundred and seventeen litres.
642. One hundred and eighteen litres.
643. One hundred and nineteen litres.
644. One hundred and twenty litres.
645. One gallon.
646. Two gallons.
647. Three gallons.
648. Four gallons.
649. Five gallons.
650. Six gallons.
651. Seven gallons.
652. Eight gallons.
653. Nine gallons.
654. Ten gallons.
655. Eleven gallons.
656. Twelve gallons.
657. Thirteen gallons.
658. Fourteen gallons.
659. Fifteen gallons.
660. Sixteen gallons.
661. Seventeen gallons.
662. Eighteen gallons.
663. Nineteen gallons.
664. Twenty gallons.
665. Twenty-one gallons.
666. Twenty-two gallons.
667. Twenty-three gallons.
668. Twenty-four gallons.
669. Twenty-five gallons.
670. Twenty-six gallons.
671. Twenty-seven gallons.
672. Twenty-eight gallons.
673. Twenty-nine gallons.
674. Thirty gallons.
675. Thirty-one gallons.
676. Thirty-two gallons.
677. One lap of fuel.
678. Two laps of fuel.
679. Car behind closing, a tenth a lap.
680. Gaining on the car ahead, two tenths a lap.
681. Car behind closing, three tenths a lap.
682. Gaining on the car ahead, four tenths a lap.
683. Car behind closing, five tenths a lap.
684. Gaining on the car ahead, six tenths a lap.
685. Car behind closing, seven tenths a lap.
686. Gaining on the car ahead, eight tenths a lap.
687. Car behind closing, nine tenths a lap.
688. Gaining on the car ahead, one second a lap.
689. Car behind closing, one point one seconds a lap.
690. Gaining on the car ahead, one point two seconds a lap.
691. Car behind closing, one point three seconds a lap.
692. Gaining on the car ahead, one point four seconds a lap.
693. Car behind closing, one point five seconds a lap.
694. Gaining on the car ahead, one point six seconds a lap.
695. Car behind closing, one point seven seconds a lap.
696. Gaining on the car ahead, one point eight seconds a lap.
697. Car behind closing, one point nine seconds a lap.
698. Gaining on the car ahead, two seconds a lap.
699. Three tenths off target.
700. Four tenths off target.
701. Five tenths off target.
702. Six tenths off target.
703. Seven tenths off target.
704. Eight tenths off target.
705. Nine tenths off target.
706. One second off target.
707. One point one seconds off target.
708. One point two seconds off target.
709. One point three seconds off target.
710. One point four seconds off target.
711. One point five seconds off target.
712. One point six seconds off target.
713. One point seven seconds off target.
714. One point eight seconds off target.
715. One point nine seconds off target.
716. Two seconds off target.

## Section N (from line 717)

Race Room, to a teammate who is next in the car. Calm and clear, a helpful heads-up.

717. Your stint starts in about fifteen minutes.
718. Your stint starts in about five minutes.

## Section O (from line 719)

Race Engineer on the radio (2.31). Short pieces of a sentence that are joined with numbers: calm, clear, like an engineer talking to his driver. Each line on its own; numbers are read as plain numbers.

719. Copy.
720. Say again.
721. Loud and clear.
722. I don't have that yet.
723. Nobody behind you.
724. Nobody ahead of you.
725. He's closing.
726. He's not closing.
727. He's pulling away.
728. You're gaining on him.
729. Out lap, warm the tyres.
730. Clear track ahead, push this lap.
731. Back off for a gap.
732. Last lap of the session.
733. Cool-down lap, bring it in.
734. Bring it in.
735. New best.
736. Consistency is improving.
737. Laps are getting scattered, settle down.
738. Good, steady laps.
739. No stop needed.
740. Urgent calls only.
741. I'll keep you posted.
742. Fuel is fine.
743. Debrief.
744. Work on.
745. Nothing stands out.
746. Pit entry.
747. Chequered flag.
748. Welcome back.
749. Green flag.
750. Fuel is now measured.
751. Target is.
752. Fuel for.
753. Laps.
754. Track temp.
755. Air temp.
756. Traffic ahead in.
757. Seconds.
758. Gap behind is.
759. Gap ahead is.
760. That's position.
761. Tenths off position.
762. A tenth off position.
763. You are position.
764. Last lap.
765. Best lap.
766. Sector.
767. Tenths down.
768. A tenth down.
769. Seconds down.
770. Litres a lap.
771. Gallons a lap.
772. Laps in the tank.
773. Laps of fuel.
774. Box in.
775. Minutes left.
776. Minute left.
777. Laps to go.
778. Lap to go.
779. Degrees.
780. Off your best.
781. You're losing most in sector.
782. Hottest tyre is the.
783. At.
784. Left front.
785. Right front.
786. Left rear.
787. Right rear.
788. Used.
789. Litres.
790. Gallons.
791. Start position.
792. Starting on.
793. Point.
794. Pole is.
795. And pole is.
796. Tenths clear.
797. Ahead is.
798. Behind is.
799. Grid.
800. Formation lap.
801. Consistency.
802. Lap.
803. Zero.
804. One.
805. Two.
806. Three.
807. Four.
808. Five.
809. Six.
810. Seven.
811. Eight.
812. Nine.
813. Ten.
814. Eleven.
815. Twelve.
816. Thirteen.
817. Fourteen.
818. Fifteen.
819. Sixteen.
820. Seventeen.
821. Eighteen.
822. Nineteen.
823. Twenty.
824. Twenty-one.
825. Twenty-two.
826. Twenty-three.
827. Twenty-four.
828. Twenty-five.
829. Twenty-six.
830. Twenty-seven.
831. Twenty-eight.
832. Twenty-nine.
833. Thirty.
834. Thirty-one.
835. Thirty-two.
836. Thirty-three.
837. Thirty-four.
838. Thirty-five.
839. Thirty-six.
840. Thirty-seven.
841. Thirty-eight.
842. Thirty-nine.
843. Forty.
844. Forty-one.
845. Forty-two.
846. Forty-three.
847. Forty-four.
848. Forty-five.
849. Forty-six.
850. Forty-seven.
851. Forty-eight.
852. Forty-nine.
853. Fifty.
854. Fifty-one.
855. Fifty-two.
856. Fifty-three.
857. Fifty-four.
858. Fifty-five.
859. Fifty-six.
860. Fifty-seven.
861. Fifty-eight.
862. Fifty-nine.
863. Sixty.
864. Sixty-one.
865. Sixty-two.
866. Sixty-three.
867. Sixty-four.
868. Sixty-five.
869. Sixty-six.
870. Sixty-seven.
871. Sixty-eight.
872. Sixty-nine.
873. Seventy.
874. Seventy-one.
875. Seventy-two.
876. Seventy-three.
877. Seventy-four.
878. Seventy-five.
879. Seventy-six.
880. Seventy-seven.
881. Seventy-eight.
882. Seventy-nine.
883. Eighty.
884. Eighty-one.
885. Eighty-two.
886. Eighty-three.
887. Eighty-four.
888. Eighty-five.
889. Eighty-six.
890. Eighty-seven.
891. Eighty-eight.
892. Eighty-nine.
893. Ninety.
894. Ninety-one.
895. Ninety-two.
896. Ninety-three.
897. Ninety-four.
898. Ninety-five.
899. Ninety-six.
900. Ninety-seven.
901. Ninety-eight.
902. Ninety-nine.
903. One hundred.
904. One hundred and one.
905. One hundred and two.
906. One hundred and three.
907. One hundred and four.
908. One hundred and five.
909. One hundred and six.
910. One hundred and seven.
911. One hundred and eight.
912. One hundred and nine.
913. One hundred and ten.
914. One hundred and eleven.
915. One hundred and twelve.
916. One hundred and thirteen.
917. One hundred and fourteen.
918. One hundred and fifteen.
919. One hundred and sixteen.
920. One hundred and seventeen.
921. One hundred and eighteen.
922. One hundred and nineteen.
923. One hundred and twenty.
924. One hundred and twenty-one.
925. One hundred and twenty-two.
926. One hundred and twenty-three.
927. One hundred and twenty-four.
928. One hundred and twenty-five.
929. One hundred and twenty-six.
930. One hundred and twenty-seven.
931. One hundred and twenty-eight.
932. One hundred and twenty-nine.
933. One hundred and thirty.
934. One hundred and thirty-one.
935. One hundred and thirty-two.
936. One hundred and thirty-three.
937. One hundred and thirty-four.
938. One hundred and thirty-five.
939. One hundred and thirty-six.
940. One hundred and thirty-seven.
941. One hundred and thirty-eight.
942. One hundred and thirty-nine.
943. One hundred and forty.
944. One hundred and forty-one.
945. One hundred and forty-two.
946. One hundred and forty-three.
947. One hundred and forty-four.
948. One hundred and forty-five.
949. One hundred and forty-six.
950. One hundred and forty-seven.
951. One hundred and forty-eight.
952. One hundred and forty-nine.
953. One hundred and fifty.
