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
