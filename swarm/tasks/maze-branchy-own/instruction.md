You are inside a maze. Reach the exit.

You cannot see the maze. You explore it with one command, `./maze`, run from your working directory. Do not edit
any files; only run `./maze`. Every command prints one line of JSON.

  ./maze look           where you are, which sides are open, and which of those lead somewhere not yet explored
  ./maze move N|E|S|W   step through an open side; N is up (y - 1), E is right (x + 1)
  ./maze next-edge      shared map only: get a path to the nearest unexplored opening nobody else is heading for
  ./maze map            every cell explored so far and its open sides
  ./maze verdict        whether your run counts as a pass

`look` and `move` answer with `{"at":[x,y],"open":[...],"new":[...],"exit":false,"steps":n,"done":false}`.
`open` lists the sides you can walk through, `new` the open sides that lead into cells not yet explored, `exit` is
true when you are standing on the exit. Moving into a wall is refused and counts against you, so only move through
sides listed in `open`. Every call, refused ones too, counts toward a limit on how many calls you may make (in some
runs one limit is shared by every agent), and `done` becomes true when the run is over.

The map is yours alone: `new` only lists sides you have not explored yourself. Other agents may be in
their own copy of this maze, but you do not share anything with them. `./maze next-edge` is not available; when
nothing near you is new, walk back along your own steps to the last place that has an unexplored side.

Stop when `exit` or `done` is true, or when a command answers with an error. Then say in one line what happened.
