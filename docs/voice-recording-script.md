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
39. Turn fourteen, more speed, one down.
40. Turn fifteen, more speed, two down.
41. Turn sixteen, more speed, three down.
42. Turn seventeen, more speed, four down.
43. Turn eighteen, more speed, five down.
44. Turn nineteen, more speed, six down.
45. Turn twenty, more speed, seven down.
46. Turn twenty-one, more speed, eight down.
47. Turn twenty-two, more speed, nine down.
48. Turn twenty-three, more speed, ten down.
49. Turn twenty-four, more speed, eleven down.
50. Turn twenty-five, more speed, twelve down.
51. Turn one, more speed, thirteen down.
52. Turn two, more speed, fourteen down.
53. Turn three, more speed, fifteen down.
54. Turn four, more speed, sixteen down.
55. Turn five, more speed, seventeen down.
56. Turn six, more speed, eighteen down.
57. Turn seven, more speed, nineteen down.
58. Turn eight, more speed, twenty down.

## Section C (from line 59)

Lap times, slower than your best. Flat and matter-of-fact.

59. Down a tenth.
60. Down two tenths.
61. Down three tenths.
62. Down four tenths.
63. Down five tenths.
64. Down six tenths.
65. Down seven tenths.
66. Down eight tenths.
67. Down nine tenths.
68. Down one second.
69. Down one point one seconds.
70. Down one point two seconds.
71. Down one point three seconds.
72. Down one point four seconds.
73. Down one point five seconds.
74. Down one point six seconds.
75. Down one point seven seconds.
76. Down one point eight seconds.
77. Down one point nine seconds.
78. Down two seconds.

## Section D (from line 79)

Lap times, a new best. A little lift, but stay calm. Short pause at the comma.

79. New best, up a tenth.
80. New best, up two tenths.
81. New best, up three tenths.
82. New best, up four tenths.
83. New best, up five tenths.
84. New best, up six tenths.
85. New best, up seven tenths.
86. New best, up eight tenths.
87. New best, up nine tenths.
88. New best, up one second.
89. New best, up one point one seconds.
90. New best, up one point two seconds.
91. New best, up one point three seconds.
92. New best, up one point four seconds.
93. New best, up one point five seconds.
94. New best, up one point six seconds.
95. New best, up one point seven seconds.
96. New best, up one point eight seconds.
97. New best, up one point nine seconds.
98. New best, up two seconds.

## Section E (from line 99)

Brake pressure. First 'Good, eighty-four' with a short pause at the comma, then each number on its own.

99. Good, eighty-four.
100. Zero.
101. One.
102. Two.
103. Three.
104. Four.
105. Five.
106. Six.
107. Seven.
108. Eight.
109. Nine.
110. Ten.
111. Eleven.
112. Twelve.
113. Thirteen.
114. Fourteen.
115. Fifteen.
116. Sixteen.
117. Seventeen.
118. Eighteen.
119. Nineteen.
120. Twenty.
121. Twenty-one.
122. Twenty-two.
123. Twenty-three.
124. Twenty-four.
125. Twenty-five.
126. Twenty-six.
127. Twenty-seven.
128. Twenty-eight.
129. Twenty-nine.
130. Thirty.
131. Thirty-one.
132. Thirty-two.
133. Thirty-three.
134. Thirty-four.
135. Thirty-five.
136. Thirty-six.
137. Thirty-seven.
138. Thirty-eight.
139. Thirty-nine.
140. Forty.
141. Forty-one.
142. Forty-two.
143. Forty-three.
144. Forty-four.
145. Forty-five.
146. Forty-six.
147. Forty-seven.
148. Forty-eight.
149. Forty-nine.
150. Fifty.
151. Fifty-one.
152. Fifty-two.
153. Fifty-three.
154. Fifty-four.
155. Fifty-five.
156. Fifty-six.
157. Fifty-seven.
158. Fifty-eight.
159. Fifty-nine.
160. Sixty.
161. Sixty-one.
162. Sixty-two.
163. Sixty-three.
164. Sixty-four.
165. Sixty-five.
166. Sixty-six.
167. Sixty-seven.
168. Sixty-eight.
169. Sixty-nine.
170. Seventy.
171. Seventy-one.
172. Seventy-two.
173. Seventy-three.
174. Seventy-four.
175. Seventy-five.
176. Seventy-six.
177. Seventy-seven.
178. Seventy-eight.
179. Seventy-nine.
180. Eighty.
181. Eighty-one.
182. Eighty-two.
183. Eighty-three.
184. Eighty-four.
185. Eighty-five.
186. Eighty-six.
187. Eighty-seven.
188. Eighty-eight.
189. Eighty-nine.
190. Ninety.
191. Ninety-one.
192. Ninety-two.
193. Ninety-three.
194. Ninety-four.
195. Ninety-five.
196. Ninety-six.
197. Ninety-seven.
198. Ninety-eight.
199. Ninety-nine.
200. One hundred.

## Section F (from line 201)

Short practice cues. Each one on its own.

201. Good brake point.
202. Good, held it in.
203. Good, no coasting.
204. Good throttle.
205. Good speed.
206. Good, on pace.
207. Too much brake.
208. Braked too early.
209. Braked too late.
210. Hold the brake in.
211. Less coasting.
212. More speed.
213. New best.
