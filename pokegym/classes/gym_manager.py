

class Gym:
    def __init__(self, events):
        self.events = events
        self.gym_maps = {
            0: [166, 8], # [54, 2], # pewter2
            1: [166, 8], # [65, 3], # Cerulean3
            2: [92, 5, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104], # vermilion5
            3: [134, 6], # celadon6
            4: [157, 7], # Fuchsia7
            5: [178, 10], # Saffron10
            6: [166, 8], # Cinnabar8
            7: [45, ], # viridian1
        }


    @property
    def gym_prog(self): 
        gym_prog_list = []
        gym_list = ['EVENT_BEAT_BROCK', 'EVENT_BEAT_MISTY', 'EVENT_BEAT_LT_SURGE', 'EVENT_BEAT_ERIKA', 'EVENT_BEAT_KOGA', 'EVENT_BEAT_SABRINA', 'EVENT_BEAT_BLAINE', 'EVENT_BEAT_VIRIDIAN_GYM_GIOVANNI']
        for i in gym_list:
            gym_prog_list.append(self.events.get_event(i))
        return gym_prog_list
    
    def maps(self):
        high = []
        low = []
        for i in range(len(self.gym_prog)):
            if self.gym_prog[i]:
                low += self.gym_maps[i]
            else:
                high += self.gym_maps[i]
        return high, low